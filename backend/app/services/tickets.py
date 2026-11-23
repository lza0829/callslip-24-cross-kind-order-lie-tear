from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from fastapi import HTTPException
from app.config import ORDER_RULE
from app.services.core import (
    business_day,
    is_sealed,
    kind_row,
    kind_summaries,
    now,
    parse_kind_ids,
    require_open_day,
    ticket_dict,
    ticket_row,
    window_row,
)
from app.services import order_audit


@contextmanager
def _write(conn):
    """写口事务：未在事务则 BEGIN IMMEDIATE 串行化并发；再套 SAVEPOINT。

    任一步抛错（含次序闸 409）都回滚到本笔起点——跳号整笔回滚，
    绝不出现「回执绿但号牌/窗口/审计已写一半」的半截。
    """
    started = False
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise HTTPException(409, detail="写通道繁忙，请重试")
        started = True
    conn.execute("SAVEPOINT call_op")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK TO call_op")
        conn.execute("RELEASE call_op")
        if started:
            conn.rollback()
        raise
    else:
        conn.execute("RELEASE call_op")
        if started:
            # 写与次序闸同一次提交：要么整笔落库且审计闭合，要么整笔不见
            conn.commit()


def _insert_event(conn, ticket_id, window_id, kind_id, action, ts, snapshot="") -> int:
    cur = conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
           VALUES (?,?,?,?,?,'',?)""",
        (ticket_id, window_id, kind_id, action, ts, snapshot),
    )
    return int(cur.lastrowid)


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status == "等候":
        # 序号页与叫号入口同源：按全局取号时刻（并列以号牌id破）排列，不按种类内位次。
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND status='等候' ORDER BY taken_at, id""",
            (d,),
        ).fetchall()
        out = [ticket_dict(conn, r) for r in rows]
        assignments, audit = _surface_evidence(conn, d)
        for i, t in enumerate(out, 1):
            t["次序"] = i
            t["可叫窗口"] = assignments.get(t["id"], [])
            t["可叫"] = bool(t["可叫窗口"])
        for t in out:
            t["次序审计一致"] = audit["一致"]
        return out
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY taken_at, id",
            (d, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? ORDER BY id DESC",
            (d,),
        ).fetchall()
    return [ticket_dict(conn, r) for r in rows]


def pick_next(conn, day: str, allowed: list[int]):
    """跨种类选号谓词（全局唯一口径：取号时刻FIFO）。

    在窗口可办种类交集内，按 (取号时刻, 号牌id) 升序取最早者；
    不因种类偏好跳号。叫号、窗口下一号预览、等候墙、重放校验共用本谓词。
    返回 (选中行, 可办种类内等候候选数)。
    """
    if not allowed:
        return None, 0
    ph = ",".join("?" * len(allowed))
    params: list = [day, *allowed]
    cnt = conn.execute(
        f"""SELECT COUNT(*) AS c FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({ph})""",
        params,
    ).fetchone()["c"]
    if not cnt:
        return None, 0
    row = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status='等候' AND kind_id IN ({ph})
            ORDER BY taken_at, id LIMIT 1""",
        params,
    ).fetchone()
    return row, cnt


def _next_assignments(conn, day: str) -> dict[int, list[str]]:
    """每个开放空闲窗口按同一谓词选出的下一号 -> 可叫窗口名列表。

    墙高亮、序号页「可叫」、可叫集合接口全部读这里，绝不另算一套。
    """
    out: dict[int, list[str]] = {}
    if is_sealed(conn, day):
        return out
    wins = conn.execute(
        """SELECT id, name, kind_ids FROM windows
           WHERE open=1 AND current_ticket_id IS NULL ORDER BY id"""
    ).fetchall()
    for w in wins:
        allowed = parse_kind_ids(w["kind_ids"])
        row, _ = pick_next(conn, day, allowed)
        if row:
            out.setdefault(row["id"], []).append(w["name"])
    return out


def _surface_evidence(conn, day: str) -> tuple[dict[int, list[str]], dict]:
    """墙/序号页/总览共用的同源证据：可叫集合 + 次序审计结果。"""
    return _next_assignments(conn, day), order_audit.replay(conn, day)


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    assignments, audit = _surface_evidence(conn, day)
    wall = []
    waiting_order: list[dict] = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, taken_at, id""",
            (day, k["id"]),
        ).fetchall()
        items = []
        for r in rows:
            t = ticket_dict(conn, r)
            t["可叫窗口"] = assignments.get(t["id"], [])
            # 高亮只由可叫集合谓词决定，不来自任何缓存或回执
            t["高亮"] = bool(t["可叫窗口"])
            items.append(t)
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": items,
            }
        )
        for it in items:
            if it["状态"] == "等候":
                waiting_order.append(it)
    waiting_order.sort(key=lambda t: (t["取号时刻"], t["id"]))
    for i, t in enumerate(waiting_order, 1):
        t["次序"] = i

    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?
           ORDER BY t.called_at, t.id""",
        (day,),
    ).fetchall()
    open_wins = conn.execute(
        "SELECT id FROM windows WHERE open=1 ORDER BY id"
    ).fetchall()
    last_call = conn.execute(
        """SELECT e.id, e.ticket_id, e.window_id, e.created_at, t.slip_no
           FROM call_events e JOIN tickets t ON t.id=e.ticket_id
           WHERE t.business_day=? AND e.action='叫号' ORDER BY e.id DESC LIMIT 1""",
        (day,),
    ).fetchone()
    # 在办卡的高亮只能来自该号自己的最后一条叫号/重叫审计，杜绝「墙亮 A、流水记 B」。
    in_service = []
    for r in current:
        t = ticket_dict(conn, r)
        ev = conn.execute(
            """SELECT e.id, e.action, e.window_id FROM call_events e
               WHERE e.ticket_id=? AND e.action IN ('叫号','重叫')
               ORDER BY e.id DESC LIMIT 1""",
            (r["id"],),
        ).fetchone()
        t["高亮"] = bool(ev and ev["window_id"] == r["window_id"])
        if t["高亮"]:
            t["叫号事件"] = ev["id"]
        in_service.append(t)
    return {
        "营业日": day,
        "选号规则": ORDER_RULE,
        "墙面": wall,
        "叫号次序": waiting_order,
        "在办": in_service,
        "最后叫号": (
            {
                "事件": last_call["id"],
                "号牌": last_call["slip_no"],
                "窗口编号": last_call["window_id"],
                "时刻": last_call["created_at"],
            }
            if last_call
            else None
        ),
        "次序审计": {"一致": audit["一致"], "冲突数": audit["冲突数"]},
        "窗口下一号": [next_one(conn, w["id"]) for w in open_wins],
    }


def callable_set(conn) -> dict:
    """跨种类可叫集合接口：各开放窗口同一谓词的下一号 + 全局等候次序 + 次序审计。"""
    wall = waiting_wall(conn)
    return {
        "营业日": wall["营业日"],
        "规则": ORDER_RULE,
        "窗口下一号": wall["窗口下一号"],
        "等候次序": wall["叫号次序"],
        "次序审计": wall["次序审计"],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT kind_ids FROM windows WHERE open=1").fetchall()
    if not any(kind_id in parse_kind_ids(w["kind_ids"]) for w in open_wins):
        raise HTTPException(400, detail="暂无开放窗口可办该业务种类，禁止取号")
    with _write(conn):
        seq = kind["seq"] + 1
        slip = f"{kind['prefix']}{seq:03d}"
        # 位次物化：按当日+种类全状态取最大位次+1，叫走/办结后新号不再撞位次
        pos_row = conn.execute(
            "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=?",
            (day, kind_id),
        ).fetchone()
        pos = pos_row["m"] + 1
        ts = now()
        cur = conn.execute(
            """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
               VALUES (?,?,?,'等候',NULL,?,?,?)""",
            (slip, kind_id, day, pos, ts, ""),
        )
        tid = int(cur.lastrowid)
        conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
        # 取号即入审计流：事件时刻就是选号谓词的排序键，供跨种类次序重放。
        _insert_event(
            conn,
            tid,
            None,
            kind_id,
            "取号",
            ts,
            order_audit.rule_snapshot([kind_id], 1, 位次=pos),
        )
        order_audit.assert_consistent(conn, day, context="取号")
    return ticket_dict(conn, ticket_row(conn, tid))


def call_next(conn, window_id: int) -> dict:
    """叫下一号：全局取号时刻FIFO，可办种类交集内最早者。

    事务 BEGIN IMMEDIATE 串行化并发叫号；条件更新行数校验保证
    并发双叫不抢同号、半截叫号不留错种类占窗。写口在提交前用
    审计重放做次序闸：跳号整笔回滚，绝不返回「成功」的假回执。
    """
    day = require_open_day(conn)
    with _write(conn):
        win = window_row(conn, window_id)
        if not win["open"]:
            raise HTTPException(400, detail="窗口已关闭")
        if win["current_ticket_id"]:
            raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
        allowed = parse_kind_ids(win["kind_ids"])
        if not allowed:
            raise HTTPException(400, detail="本窗口未配置可办种类")
        row, candidates = pick_next(conn, day, allowed)
        if not row:
            raise HTTPException(400, detail="可办种类内暂无等候号可叫")
        tid = row["id"]
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=? AND status='等候'",
            (window_id, ts, tid),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, detail="该号刚被其他窗口叫走，请重试")
        cur = conn.execute(
            "UPDATE windows SET current_ticket_id=? WHERE id=? AND current_ticket_id IS NULL",
            (tid, window_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(400, detail="窗口仍有在办号，请先办结或过号")
        evid = _insert_event(
            conn,
            tid,
            window_id,
            row["kind_id"],
            "叫号",
            ts,
            order_audit.rule_snapshot(allowed, candidates),
        )
        audit = order_audit.assert_consistent(conn, day, context="叫号")
    out = ticket_dict(conn, ticket_row(conn, tid))
    # 叫号回执：携带谓词输入与审计事件号，凭此可重放本次跨种类选号次序
    out["回执"] = {
        "规则": ORDER_RULE,
        "窗口": win["name"],
        "窗口编号": window_id,
        "可办种类": kind_summaries(conn, allowed),
        "候选数": candidates,
        "号牌": out["号牌"],
        "业务种类": out["业务种类"],
        "取号时刻": out["取号时刻"],
        "叫号时刻": ts,
        "次序证据": {
            "规则": order_audit.ORDER_RULE_ID,
            "事件": evid,
            "期望号牌": row["slip_no"],
            "叫到号牌": out["号牌"],
            "可办种类": allowed,
            "候选数": candidates,
            "一致": True,
        },
    }
    out["次序"] = "一致"
    out["审计"] = {
        "一致": audit["一致"],
        "检查数": audit["检查数"],
        "冲突数": audit["冲突数"],
    }
    return out


def next_one(conn, window_id: int) -> dict:
    """窗口下一号预览：与叫号入口同一谓词，只读不改状态。"""
    day = business_day(conn)
    win = window_row(conn, window_id)
    allowed = parse_kind_ids(win["kind_ids"])
    base = {
        "窗口编号": window_id,
        "窗口": win["name"],
        "可办种类": kind_summaries(conn, allowed),
        "规则": ORDER_RULE,
        "候选数": 0,
        "下一号": None,
    }
    if is_sealed(conn, day):
        return {**base, "可叫": False, "拒因": "当日已封口"}
    if not win["open"]:
        return {**base, "可叫": False, "拒因": "窗口已关闭"}
    if win["current_ticket_id"]:
        return {**base, "可叫": False, "拒因": "窗口仍有在办号"}
    if not allowed:
        return {**base, "可叫": False, "拒因": "本窗口未配置可办种类"}
    row, candidates = pick_next(conn, day, allowed)
    if not row:
        return {**base, "可叫": False, "拒因": "可办种类内暂无等候号"}
    return {
        **base,
        "可叫": True,
        "拒因": None,
        "候选数": candidates,
        "选取依据": {"取号时刻": row["taken_at"], "号牌id": row["id"]},
        "下一号": ticket_dict(conn, row),
    }


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    with _write(conn):
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != day:
            raise HTTPException(400, detail="非当日号牌")
        if row["status"] not in ("叫号中", "办理中"):
            raise HTTPException(400, detail="仅在办或叫号中可过号")
        wid = row["window_id"]
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=? AND status IN ('叫号中','办理中')",
            (ticket_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, detail="该号状态已变化，请刷新后重试")
        if wid:
            conn.execute(
                "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
                (wid, ticket_id),
            )
        _insert_event(conn, ticket_id, wid, row["kind_id"], "过号", ts)
        order_audit.assert_consistent(conn, day, context="过号")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    day = require_open_day(conn)
    with _write(conn):
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != day:
            raise HTTPException(400, detail="非当日号牌")
        if row["status"] != "过号":
            raise HTTPException(400, detail="仅过号可重叫")
        wid = window_id or row["window_id"]
        if not wid:
            raise HTTPException(400, detail="须指定窗口重叫")
        win = window_row(conn, wid)
        if not win["open"]:
            raise HTTPException(400, detail="窗口已关闭")
        if win["current_ticket_id"]:
            raise HTTPException(400, detail="窗口仍有在办号")
        allowed = parse_kind_ids(win["kind_ids"])
        if row["kind_id"] not in allowed:
            raise HTTPException(400, detail="本窗口不可办该业务种类")
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=? AND status='过号'",
            (wid, ts, ticket_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, detail="该号状态已变化，请刷新后重试")
        cur = conn.execute(
            "UPDATE windows SET current_ticket_id=? WHERE id=? AND current_ticket_id IS NULL",
            (ticket_id, wid),
        )
        if cur.rowcount != 1:
            raise HTTPException(400, detail="窗口仍有在办号")
        _insert_event(
            conn,
            ticket_id,
            wid,
            row["kind_id"],
            "重叫",
            ts,
            order_audit.rule_snapshot(allowed, 1),
        )
        order_audit.assert_consistent(conn, day, context="重叫")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    with _write(conn):
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != day:
            raise HTTPException(400, detail="非当日号牌")
        if row["status"] not in ("叫号中", "办理中"):
            raise HTTPException(400, detail="仅在办或叫号中可办结")
        wid = row["window_id"]
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='已办结', finished_at=? WHERE id=? AND status IN ('叫号中','办理中')",
            (ts, ticket_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, detail="该号状态已变化，请刷新后重试")
        if wid:
            conn.execute(
                "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
                (wid, ticket_id),
            )
        _insert_event(conn, ticket_id, wid, row["kind_id"], "办结", ts)
        order_audit.assert_consistent(conn, day, context="办结")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    with _write(conn):
        row = ticket_row(conn, ticket_id)
        if row["business_day"] != day:
            raise HTTPException(400, detail="非当日号牌")
        if row["status"] not in ("等候", "过号"):
            raise HTTPException(400, detail="仅等候或过号可作废")
        ts = now()
        cur = conn.execute(
            "UPDATE tickets SET status='已作废', finished_at=? WHERE id=? AND status IN ('等候','过号')",
            (ts, ticket_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, detail="该号状态已变化，请刷新后重试")
        # 作废也入叫号事件流（无窗口）：等候池出队须可重放
        _insert_event(conn, ticket_id, None, row["kind_id"], "作废", ts)
        order_audit.assert_consistent(conn, day, context="作废")
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, t.slip_no, w.name AS window_name, k.name AS kind_name
           FROM call_events e
           JOIN tickets t ON t.id = e.ticket_id
           LEFT JOIN windows w ON w.id = e.window_id
           LEFT JOIN business_kinds k ON k.id = e.kind_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "号牌": r["slip_no"],
            "窗口": r["window_name"] or "—",
            "业务种类": r["kind_name"] or "",
            "动作": r["action"],
            "时刻": r["created_at"],
            "备注": r["note"],
            "快照": r["snapshot"],
        }
        for r in rows
    ]


def verify_order(conn, day: str | None = None) -> dict:
    """重放校验：按事件流重放选号谓词与状态同命运，冲突即未闭合。"""
    return order_audit.replay(conn, day)

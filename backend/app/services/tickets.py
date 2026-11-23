from __future__ import annotations
import json
import sqlite3
from fastapi import HTTPException
from app.config import ORDER_RULE, ORDER_RULE_ID
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
from app.services import order_surface


def list_tickets(conn, status: str | None = None, day: str | None = None) -> list[dict]:
    d = day or business_day(conn)
    if status:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE business_day=? AND status=? ORDER BY queue_pos, id",
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


def _rule_snapshot(allowed: list[int], candidates: int) -> str:
    return json.dumps(
        {"规则": ORDER_RULE_ID, "可办种类": allowed, "候选数": candidates},
        ensure_ascii=False,
    )


def waiting_wall(conn) -> dict:
    day = business_day(conn)
    kinds = conn.execute(
        "SELECT id, name, prefix FROM business_kinds WHERE active=1 ORDER BY id"
    ).fetchall()
    wall = []
    for k in kinds:
        rows = conn.execute(
            """SELECT * FROM tickets
               WHERE business_day=? AND kind_id=? AND status IN ('等候','过号')
               ORDER BY CASE status WHEN '等候' THEN 0 ELSE 1 END, queue_pos, id""",
            (day, k["id"]),
        ).fetchall()
        wall.append(
            {
                "业务种类": k["name"],
                "前缀": k["prefix"],
                "等候数": sum(1 for r in rows if r["status"] == "等候"),
                "过号数": sum(1 for r in rows if r["status"] == "过号"),
                "队列": [ticket_dict(conn, r) for r in rows],
            }
        )
    current = conn.execute(
        """SELECT t.* FROM tickets t
           JOIN windows w ON w.current_ticket_id = t.id
           WHERE t.business_day=?""",
        (day,),
    ).fetchall()
    # 窗口下一号：与叫号入口共用同一选号谓词，墙面上即可重放跨种类出队次序
    open_wins = conn.execute(
        "SELECT id FROM windows WHERE open=1 ORDER BY id"
    ).fetchall()
    return {
        "营业日": day,
        "选号规则": ORDER_RULE,
        "墙面": wall,
        "在办": [ticket_dict(conn, r) for r in current],
        "窗口下一号": [next_one(conn, w["id"]) for w in open_wins],
    }


def take_ticket(conn, kind_id: int) -> dict:
    day = require_open_day(conn)
    kind = kind_row(conn, kind_id)
    if not kind["active"]:
        raise HTTPException(400, detail="该业务种类已停用")
    open_wins = conn.execute("SELECT kind_ids FROM windows WHERE open=1").fetchall()
    if not any(kind_id in parse_kind_ids(w["kind_ids"]) for w in open_wins):
        raise HTTPException(400, detail="暂无开放窗口可办该业务种类，禁止取号")
    seq = kind["seq"] + 1
    slip = f"{kind['prefix']}{seq:03d}"
    # 位次物化：按当日+种类全状态取最大位次+1，叫走/办结后新号不再撞位次
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(queue_pos),0) AS m FROM tickets WHERE business_day=? AND kind_id=?",
        (day, kind_id),
    ).fetchone()
    pos = pos_row["m"] + 1
    cur = conn.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,?)""",
        (slip, kind_id, day, pos, now(), ""),
    )
    conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (seq, kind_id))
    return ticket_dict(conn, ticket_row(conn, cur.lastrowid))


def call_next(conn, window_id: int) -> dict:
    """叫下一号：全局取号时刻FIFO，可办种类交集内最早者。

    事务 BEGIN IMMEDIATE 串行化并发叫号；条件更新行数校验保证
    并发双叫不抢同号、半截叫号不留错种类占窗。
    """
    day = require_open_day(conn)
    if not conn.in_transaction:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise HTTPException(409, detail="叫号通道繁忙，请重试")
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
    conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
           VALUES (?,?,?,'叫号',?,'',?)""",
        (tid, window_id, row["kind_id"], ts, _rule_snapshot(allowed, candidates)),
    )
    out = ticket_dict(conn, ticket_row(conn, tid))
    # 叫号回执：携带谓词输入，凭此可重放本次跨种类选号次序
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
        "下一号": ticket_dict(conn, row),
    }


def mark_skip(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可过号")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='过号', finished_at=NULL WHERE id=?",
        (ticket_id,),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note)
               VALUES (?,?,?,'过号',?,'')""",
            (ticket_id, wid, row["kind_id"], ts),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def recall(conn, ticket_id: int, window_id: int | None = None) -> dict:
    day = require_open_day(conn)
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
    conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
           VALUES (?,?,?,'重叫',?,'',?)""",
        (ticket_id, wid, row["kind_id"], ts, _rule_snapshot(allowed, 1)),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def finish(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("叫号中", "办理中"):
        raise HTTPException(400, detail="仅在办或叫号中可办结")
    wid = row["window_id"]
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已办结', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    if wid:
        conn.execute(
            "UPDATE windows SET current_ticket_id=NULL WHERE id=? AND current_ticket_id=?",
            (wid, ticket_id),
        )
        conn.execute(
            """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note)
               VALUES (?,?,?,'办结',?,'')""",
            (ticket_id, wid, row["kind_id"], ts),
        )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def void_ticket(conn, ticket_id: int) -> dict:
    day = require_open_day(conn)
    row = ticket_row(conn, ticket_id)
    if row["business_day"] != day:
        raise HTTPException(400, detail="非当日号牌")
    if row["status"] not in ("等候", "过号"):
        raise HTTPException(400, detail="仅等候或过号可作废")
    ts = now()
    conn.execute(
        "UPDATE tickets SET status='已作废', finished_at=? WHERE id=?",
        (ts, ticket_id),
    )
    # 作废也入叫号事件流（无窗口）：等候池出队须可重放
    conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note)
           VALUES (?,NULL,?,'作废',?,'')""",
        (ticket_id, row["kind_id"], ts),
    )
    return ticket_dict(conn, ticket_row(conn, ticket_id))


def list_events(conn, limit: int = 40) -> list[dict]:
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
        }
        for r in rows
    ]


def _snapshot_allowed(conn, ev) -> list[int]:
    """事件记录时的窗口可办种类快照；缺快照（旧数据）回退为窗口当前配置。"""
    if ev["snapshot"]:
        try:
            ids = json.loads(ev["snapshot"]).get("可办种类")
            if isinstance(ids, list):
                return [int(x) for x in ids]
        except (ValueError, AttributeError):
            pass
    if ev["window_id"]:
        win = conn.execute(
            "SELECT kind_ids FROM windows WHERE id=?", (ev["window_id"],)
        ).fetchone()
        if win:
            return parse_kind_ids(win["kind_ids"])
    return []


def verify_order(conn, day: str | None = None) -> dict:
    """重放校验：按事件流重放选号谓词，逐条核对跨种类出队次序。

    任一叫号事件叫到可办种类外的号、或未叫 (取号时刻, 号牌id) 最早者，
    即判冲突——次序不可重放视为未闭合。
    """
    d = day or business_day(conn)
    tickets = conn.execute(
        """SELECT id, slip_no, kind_id, taken_at FROM tickets
           WHERE business_day=? ORDER BY taken_at, id""",
        (d,),
    ).fetchall()
    events = conn.execute(
        """SELECT e.*, t.slip_no, t.kind_id AS ticket_kind
           FROM call_events e JOIN tickets t ON t.id = e.ticket_id
           WHERE t.business_day=? ORDER BY e.id""",
        (d,),
    ).fetchall()
    waiting: dict[int, sqlite3.Row] = {}
    ti = 0
    details = []
    ok = True
    checked = 0

    def admit(upto: str) -> None:
        nonlocal ti
        while ti < len(tickets) and tickets[ti]["taken_at"] <= upto:
            waiting[tickets[ti]["id"]] = tickets[ti]
            ti += 1

    for ev in events:
        admit(ev["created_at"])
        if ev["action"] == "叫号":
            allowed = _snapshot_allowed(conn, ev)
            cands = [t for t in waiting.values() if t["kind_id"] in allowed]
            expect = (
                min(cands, key=lambda t: (t["taken_at"], t["id"])) if cands else None
            )
            got = ev["ticket_id"]
            coverage = ev["ticket_kind"] in allowed
            match = expect is not None and expect["id"] == got
            checked += 1
            ok = ok and match and coverage
            if not coverage:
                reason = "叫到不可办种类"
            elif not match:
                reason = f"应叫 {expect['slip_no']}" if expect else "该号不在等候池"
            else:
                reason = ""
            details.append(
                {
                    "事件": ev["id"],
                    "动作": "叫号",
                    "号牌": ev["slip_no"],
                    "窗口编号": ev["window_id"],
                    "期望号牌": expect["slip_no"] if expect else None,
                    "结果": "一致" if (match and coverage) else "冲突",
                    "说明": reason,
                }
            )
            waiting.pop(got, None)
        elif ev["action"] == "作废":
            waiting.pop(ev["ticket_id"], None)
        elif ev["action"] == "重叫":
            allowed = _snapshot_allowed(conn, ev)
            coverage = ev["ticket_kind"] in allowed
            checked += 1
            ok = ok and coverage
            details.append(
                {
                    "事件": ev["id"],
                    "动作": "重叫",
                    "号牌": ev["slip_no"],
                    "窗口编号": ev["window_id"],
                    "期望号牌": ev["slip_no"],
                    "结果": "一致" if coverage else "冲突",
                    "说明": "" if coverage else "叫到不可办种类",
                }
            )
    return {
        "营业日": d,
        "规则": ORDER_RULE,
        "一致": ok,
        "检查数": checked,
        "明细": details,
    }

_call_next_impl = call_next


def call_next(*args, **kwargs):
    out = _call_next_impl(*args, **kwargs)
    return order_surface.after_call(args[0] if args else None, out, *args[1:], **kwargs)


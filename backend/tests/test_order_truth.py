"""假次序收口夹具：回执、墙高亮、序号页、总览桶、审计必须同源同命运。

架构一（全局取号时刻 FIFO）下，写口以审计重放为次序闸：跳号整笔回滚，
展示层只读同一 replay 结果，不得出现「回执绿但次序证据撒谎」。
"""
from __future__ import annotations
import json

import pytest
from fastapi import HTTPException

import app.db as appdb
from app.seed import seed_if_empty
from app.services import dayseal
from app.services import tickets as tic
from app.services import windows as win_svc


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(appdb, "DB_PATH", tmp_path / "t.db")
    seed_if_empty()
    with appdb.db() as c:
        c.execute("DELETE FROM call_events")
        c.execute("DELETE FROM tickets")
        c.execute("UPDATE windows SET current_ticket_id=NULL, open=1")
        c.execute("UPDATE business_kinds SET seq=0")
        c.commit()
        yield c


def kids(c) -> dict:
    return {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM business_kinds")}


def insert_ticket(c, kid, slip, taken_at, pos=1, status="等候"):
    day = tic.business_day(c)
    c.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,?,NULL,?,?,'')""",
        (slip, kid, day, status, pos, taken_at),
    )
    return c.execute("SELECT id FROM tickets WHERE slip_no=?", (slip,)).fetchone()["id"]


def forge_call(c, tid, wid, kind_id, allowed, ts):
    """模拟旧缺陷：写口跳号、直接落叫号事件但越过更早异种类号牌。"""
    conn = c
    conn.execute(
        "UPDATE tickets SET status='办理中', window_id=?, called_at=? WHERE id=?",
        (wid, ts, tid),
    )
    conn.execute("UPDATE windows SET current_ticket_id=? WHERE id=?", (tid, wid))
    conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, snapshot)
           VALUES (?,?,?,'叫号',?,?)""",
        (tid, wid, kind_id, ts, json.dumps({"规则": "taken_at_fifo", "可办种类": allowed, "候选数": 5})),
    )


# A. 跳号夹具：异种类更早号牌被越过，回执不得再宣称成功/正常
def test_skipped_earlier_other_kind_is_not_green(conn):
    k = kids(conn)
    early = insert_ticket(conn, k["个人储蓄"], "储901", "2026-01-01T09:00:00.000000")  # 更早
    later = insert_ticket(conn, k["对公结算"], "对901", "2026-01-01T09:05:00.000000")  # 异种类但更晚
    forge_call(conn, later, 3, k["对公结算"], [k["个人储蓄"], k["对公结算"], k["理财咨询"]], "2026-01-01T09:06:00.000000")

    res = tic.verify_order(conn)
    assert res["一致"] is False
    conflict = [d for d in res["明细"] if d["结果"] == "冲突"][0]
    assert "应叫 储901" in conflict["说明"]

    # 墙、总览、序号页同源暴露冲突，绝不显示为正常办理
    wall = tic.waiting_wall(conn)
    assert wall["次序审计"]["一致"] is False
    ov = dayseal.overview(conn)
    assert ov["次序审计"]["一致"] is False and ov["次序审计"]["冲突数"] >= 1
    waiting = tic.list_tickets(conn, status="等候")
    assert all(r["次序审计一致"] is False for r in waiting)


# B. 写口次序闸：半截/假次序存在时，新叫号整笔回滚，且不得只改高亮
def test_write_gate_rolls_back_whole_call(conn):
    k = kids(conn)
    insert_ticket(conn, k["个人储蓄"], "储001", "2026-01-01T09:00:00.000000")
    insert_ticket(conn, k["个人储蓄"], "储002", "2026-01-01T09:01:00.000000")
    first = tic.call_next(conn, 3)
    assert first["号牌"] == "储001"
    # 半截写入：号牌已办结却没有办结审计事件，窗口仍占着它
    conn.execute("UPDATE tickets SET status='已办结' WHERE id=?", (first["id"],))
    before_events = conn.execute("SELECT COUNT(*) AS c FROM call_events").fetchone()["c"]
    before_status = conn.execute("SELECT status FROM tickets WHERE slip_no='储002'").fetchone()["status"]

    with pytest.raises(HTTPException) as ex:
        tic.call_next(conn, 2)
    assert ex.value.status_code == 409
    assert "次序" in ex.value.detail
    # 整笔回滚：没有新事件、储002 仍在等候、窗口2 未被占
    assert conn.execute("SELECT COUNT(*) AS c FROM call_events").fetchone()["c"] == before_events
    assert conn.execute("SELECT status FROM tickets WHERE slip_no='储002'").fetchone()["status"] == before_status
    assert conn.execute("SELECT current_ticket_id FROM windows WHERE id=2").fetchone()["current_ticket_id"] is None
    assert conn.execute("SELECT current_ticket_id FROM windows WHERE id=3").fetchone()["current_ticket_id"] == first["id"]


# C. 墙高亮与审计号一致：高亮只来自可叫集合谓词、在办高亮只来自最后叫号事件
def test_wall_highlight_matches_audit_and_predicate(conn):
    k = kids(conn)
    insert_ticket(conn, k["个人储蓄"], "储001", "2026-01-01T09:00:00.000000")  # 最早
    insert_ticket(conn, k["对公结算"], "对001", "2026-01-01T09:01:00.000000")
    insert_ticket(conn, k["理财咨询"], "理001", "2026-01-01T09:02:00.000000")
    out = tic.call_next(conn, 2)  # 窗口2可办[对,储]，FIFO 应叫储001（异种类更早者不被越过）
    assert out["号牌"] == "储001"
    ev = conn.execute("SELECT id FROM call_events WHERE action='叫号'").fetchone()["id"]
    assert out["回执"]["次序证据"]["事件"] == ev
    assert out["回执"]["次序证据"]["期望号牌"] == out["回执"]["次序证据"]["叫到号牌"] == "储001"

    wall = tic.waiting_wall(conn)
    # 在办高亮与审计号一致
    mine = [t for t in wall["在办"] if t["号牌"] == "储001"][0]
    assert mine["高亮"] is True and mine["叫号事件"] == ev
    assert wall["最后叫号"]["事件"] == ev and wall["最后叫号"]["号牌"] == "储001"
    # 等候区高亮 == 各窗口下一号谓词结果（墙与谓词同一集合，绝不多亮/少亮）
    next_slips = {n["下一号"]["号牌"] for n in wall["窗口下一号"] if n["可叫"]}
    lit = {t["号牌"] for b in wall["墙面"] for t in b["队列"] if t["高亮"]}
    assert lit == next_slips
    # 全局最早等候者（对001）必须被某个窗口的谓词选中并可叫
    assert "对001" in lit
    assert "储001" not in lit  # 已被叫走，不得仍在等候区高亮
    # 墙与实叫谓词一致
    prev = tic.next_one(conn, 3)
    assert prev["可叫"] and prev["下一号"]["号牌"] == "对001"


# D. 序号页不得仍显示更早号可叫：按全局取号时刻排序，可叫来自同一谓词
def test_number_page_order_and_callable(conn):
    k = kids(conn)
    insert_ticket(conn, k["个人储蓄"], "储001", "2026-01-01T09:00:00.000000")
    insert_ticket(conn, k["对公结算"], "对001", "2026-01-01T09:01:00.000000")
    insert_ticket(conn, k["个人储蓄"], "储002", "2026-01-01T09:02:00.000000")
    # 只留一个开放窗口，可办=储；对001 更晚于储001，但仍不得被越过
    win_svc.set_window_kinds(conn, 3, [k["个人储蓄"]])
    rows = tic.list_tickets(conn, status="等候")
    # 排序按全局取号时刻，不按种类内位次；对001 夹在储001/储002 之间
    assert [r["号牌"] for r in rows] == ["储001", "对001", "储002"]
    assert [r["次序"] for r in rows] == [1, 2, 3]
    # 全局最早者 储001 是各开放窗口（1/2/3 都可办储）的下一号，故全部窗口都可叫它；
    # 对001 更晚，且可办对的窗口2 的下一号已是储001，故对001 此刻不可叫——
    # 「更早号可叫」这一事实与叫号入口同谓词，序号页不得越过储001 去标对001 可叫。
    assert rows[0]["可叫"] is True and set(rows[0]["可叫窗口"]) == {"一号窗", "二号窗", "三号窗"}
    assert rows[1]["号牌"] == "对001" and rows[1]["可叫"] is False
    assert rows[2]["可叫"] is False


# E. 连叫/双窗并发：次序不倒、审计闭合、回执证据与事件同号
def test_consecutive_and_concurrent_order(conn):
    k = kids(conn)
    insert_ticket(conn, k["个人储蓄"], "储001", "2026-01-01T09:00:00.000000")
    insert_ticket(conn, k["对公结算"], "对001", "2026-01-01T09:01:00.000000")
    insert_ticket(conn, k["理财咨询"], "理001", "2026-01-01T09:02:00.000000")
    a = tic.call_next(conn, 3)
    assert a["号牌"] == "储001"
    b = tic.call_next(conn, 2)  # 窗口2 可办[对,储]，储001 在办，FIFO 应 对001
    assert b["号牌"] == "对001"
    assert a["回执"]["次序证据"]["事件"] < b["回执"]["次序证据"]["事件"]
    res = tic.verify_order(conn)
    assert res["一致"] is True, res["明细"]
    assert res["检查数"] == 2


# F. 总览办中桶：办结后必须归零，不得被缓存叠高
def test_overview_bucket_truth(conn):
    k = kids(conn)
    insert_ticket(conn, k["个人储蓄"], "储001", "2026-01-01T09:00:00.000000")
    out = tic.call_next(conn, 3)
    assert dayseal.overview(conn)["办理中"] == 1
    tic.finish(conn, out["id"])
    ov = dayseal.overview(conn)
    assert ov["办理中"] == 0
    assert ov["已办结"] == 1
    assert ov["次序审计"]["一致"] is True


# G. 种子数据自启动即闭合（含在办号的叫号审计）
def test_seed_is_replayable(tmp_path, monkeypatch):
    monkeypatch.setattr(appdb, "DB_PATH", tmp_path / "seed.db")
    seed_if_empty()
    with appdb.db() as c:
        res = tic.verify_order(c)
        assert res["一致"] is True, res["明细"]
        assert res["检查数"] >= 1

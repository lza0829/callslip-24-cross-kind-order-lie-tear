"""跨种类选号次序（全局取号时刻FIFO）联动测试。

覆盖：叫号谓词、可办种类过滤、并列破、同窗不积压、并发双叫、
位次物化、叫号事件种类字段与快照、回执、窗口下一号预览、重放校验。
"""
from __future__ import annotations
import json
import threading

import pytest
from fastapi import HTTPException

import app.db as appdb
from app.seed import seed_if_empty
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
        c.commit()  # 释放写锁，避免阻塞其他连接的 BEGIN IMMEDIATE
        yield c


def kids(c) -> dict:
    return {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM business_kinds")}


def insert_ticket(c, kid, slip, pos, taken_at):
    day = tic.business_day(c)
    c.execute(
        """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, note)
           VALUES (?,?,?,'等候',NULL,?,?,'')""",
        (slip, kid, day, pos, taken_at),
    )
    return c.execute("SELECT id FROM tickets WHERE slip_no=?", (slip,)).fetchone()["id"]


def call_slip(c, wid) -> str:
    return tic.call_next(c, wid)["号牌"]


# 1. 跨种类按取号时刻FIFO：不按种类位次跳号
def test_fifo_across_kinds(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])  # 储001 位次1
    tic.take_ticket(conn, k["个人储蓄"])  # 储002 位次2
    tic.take_ticket(conn, k["对公结算"])  # 对001 位次1（种类内）
    # 旧口径按位次优先会叫 储001→对001→储002；FIFO 必须 储001→储002→对001
    first = tic.call_next(conn, 3)
    assert first["号牌"] == "储001"
    tic.finish(conn, first["id"])
    second = tic.call_next(conn, 3)
    assert second["号牌"] == "储002"
    tic.finish(conn, second["id"])
    third = tic.call_next(conn, 3)
    assert third["号牌"] == "对001"


# 2. 绝不可叫到可办种类外的号
def test_never_calls_outside_kinds(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["对公结算"])  # 窗口1可办=[储,理]，不含对公
    with pytest.raises(HTTPException) as ex:
        tic.call_next(conn, 1)
    assert ex.value.status_code == 400
    assert "暂无等候号" in ex.value.detail
    row = conn.execute("SELECT status FROM tickets WHERE slip_no='对001'").fetchone()
    assert row["status"] == "等候"  # 未被错叫占窗
    win_svc.set_window_kinds(conn, 1, [])
    with pytest.raises(HTTPException) as ex2:
        tic.call_next(conn, 1)
    assert "未配置可办种类" in ex2.value.detail


# 3. 并列取号时刻以号牌id破（id即真实先后），与种类无关
def test_tie_break_by_id(conn):
    k = kids(conn)
    ts = "2026-01-01T09:00:00.000000"
    a = insert_ticket(conn, k["对公结算"], "对901", 1, ts)
    b = insert_ticket(conn, k["个人储蓄"], "储901", 1, ts)
    got = tic.call_next(conn, 3)
    assert got["id"] == min(a, b)


# 4. 同窗不得反复只吃一种类：FIFO 下他类不积压
def test_no_starvation_single_window(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])  # 储001
    tic.take_ticket(conn, k["理财咨询"])  # 理001
    tic.take_ticket(conn, k["个人储蓄"])  # 储002
    tic.take_ticket(conn, k["个人储蓄"])  # 储003
    order = []
    for _ in range(4):
        t = tic.call_next(conn, 1)  # 窗口1可办=[储,理]
        order.append(t["号牌"])
        tic.finish(conn, t["id"])
    assert order == ["储001", "理001", "储002", "储003"]


# 5a. 并发双叫：两个窗口抢同一等候池，不得抢到同一张号
def test_concurrent_calls_distinct(conn):
    k = kids(conn)
    for _ in range(4):
        tic.take_ticket(conn, k["个人储蓄"])
    conn.commit()
    results = {}

    def worker(wid):
        try:
            with appdb.db() as c2:
                results[wid] = ("ok", tic.call_next(c2, wid)["id"])
        except HTTPException as e:
            results[wid] = ("err", e.detail)

    ts = [threading.Thread(target=worker, args=(2,)), threading.Thread(target=worker, args=(3,))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results[2][0] == "ok" and results[3][0] == "ok"
    assert results[2][1] != results[3][1]
    n = conn.execute("SELECT COUNT(*) AS c FROM call_events WHERE action='叫号'").fetchone()["c"]
    assert n == 2


# 5b. 并发双叫同一窗口：至多一成功，不留半截占窗
def test_concurrent_same_window(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])
    tic.take_ticket(conn, k["个人储蓄"])
    conn.commit()
    results = []

    def worker():
        try:
            with appdb.db() as c2:
                results.append(("ok", tic.call_next(c2, 3)["id"]))
        except HTTPException as e:
            results.append(("err", e.detail))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1
    cur = conn.execute("SELECT current_ticket_id FROM windows WHERE id=3").fetchone()
    assert cur["current_ticket_id"] == oks[0][1]  # 占窗号与叫中号一致
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM tickets WHERE status='办理中' AND window_id=3"
    ).fetchone()["c"]
    assert n == 1


# 6. 位次物化：叫走后新号不撞位次
def test_queue_pos_monotonic(conn):
    k = kids(conn)
    t1 = tic.take_ticket(conn, k["个人储蓄"])
    assert t1["位次"] == 1
    tic.call_next(conn, 1)  # 储001 转办理中，离开等候
    t2 = tic.take_ticket(conn, k["个人储蓄"])
    assert t2["位次"] == 2  # 旧口径只看等候会重回1撞位次


# 7. 回执与叫号事件种类字段、选号快照
def test_receipt_and_event_fields(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])
    out = tic.call_next(conn, 3)
    rc = out["回执"]
    assert "取号时刻" in rc["规则"]
    assert rc["候选数"] == 1
    assert [x["id"] for x in rc["可办种类"]] == [k["个人储蓄"], k["对公结算"], k["理财咨询"]]
    ev = conn.execute("SELECT * FROM call_events WHERE action='叫号'").fetchone()
    assert ev["kind_id"] == k["个人储蓄"]
    snap = json.loads(ev["snapshot"])
    assert snap["可办种类"] == [k["个人储蓄"], k["对公结算"], k["理财咨询"]]
    events = tic.list_events(conn)
    assert events[0]["业务种类"] == "个人储蓄"


# 8. 取号联动：无开放窗口可办该种类时禁止取号
def test_take_requires_serving_open_window(conn):
    k = kids(conn)
    win_svc.set_open(conn, 1, False)
    win_svc.set_open(conn, 3, False)  # 仅窗口2开放，可办=[对,储]
    with pytest.raises(HTTPException) as ex:
        tic.take_ticket(conn, k["理财咨询"])
    assert "暂无开放窗口可办" in ex.value.detail
    assert tic.take_ticket(conn, k["个人储蓄"])["号牌"] == "储001"


# 9. 窗口页种类勾选即叫号过滤唯一来源；预览与实叫一致
def test_window_kinds_edit_and_preview(conn):
    k = kids(conn)
    win_svc.set_window_kinds(conn, 3, [k["对公结算"]])
    tic.take_ticket(conn, k["个人储蓄"])
    tic.take_ticket(conn, k["对公结算"])
    prev = tic.next_one(conn, 3)
    assert prev["可叫"] and prev["下一号"]["号牌"] == "对001"
    wall = tic.waiting_wall(conn)
    mine = [n for n in wall["窗口下一号"] if n["窗口编号"] == 3]
    assert mine and mine[0]["下一号"]["号牌"] == "对001"  # 墙预览与谓词一致
    got = tic.call_next(conn, 3)
    assert got["号牌"] == "对001"  # 储001 不可办，绝不被叫
    wall2 = tic.waiting_wall(conn)
    mine2 = [n for n in wall2["窗口下一号"] if n["窗口编号"] == 3]
    assert mine2[0]["可叫"] is False and mine2[0]["拒因"] == "窗口仍有在办号"  # 同刷


# 10. 重放校验：叫/过号/重叫/作废全流程一致
def test_verify_order_ok(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])  # 储001
    tic.take_ticket(conn, k["个人储蓄"])  # 储002
    t3 = tic.take_ticket(conn, k["对公结算"])  # 对001
    first = tic.call_next(conn, 3)  # 储001
    tic.mark_skip(conn, first["id"])  # 过号
    tic.recall(conn, first["id"], 3)  # 重叫
    tic.finish(conn, first["id"])  # 办结
    tic.void_ticket(conn, t3["id"])  # 等候中作废
    assert call_slip(conn, 3) == "储002"
    res = tic.verify_order(conn)
    assert res["一致"] is True, res["明细"]
    assert res["检查数"] == 3  # 叫号×2 + 重叫×1


# 11. 重放校验：篡改出队次序（跳号）必被检出
def test_verify_detects_tamper(conn):
    k = kids(conn)
    tic.take_ticket(conn, k["个人储蓄"])  # 储001 先取
    t2 = tic.take_ticket(conn, k["个人储蓄"])  # 储002
    tic.call_next(conn, 3)
    conn.execute("UPDATE call_events SET ticket_id=? WHERE action='叫号'", (t2["id"],))
    res = tic.verify_order(conn)
    assert res["一致"] is False
    conflict = [d for d in res["明细"] if d["结果"] == "冲突"]
    assert conflict and "应叫 储001" in conflict[0]["说明"]


# 12. 重放校验：叫到可办种类外的号必被检出
def test_verify_detects_uncovered_kind(conn):
    k = kids(conn)
    t = tic.take_ticket(conn, k["对公结算"])
    conn.execute(
        """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, snapshot)
           VALUES (?,1,?,'叫号',?,?)""",
        (t["id"], k["对公结算"], tic.now(), json.dumps({"可办种类": [k["个人储蓄"]]})),
    )
    res = tic.verify_order(conn)
    assert res["一致"] is False
    assert any("不可办" in d["说明"] for d in res["明细"])

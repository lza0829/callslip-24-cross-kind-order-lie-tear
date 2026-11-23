"""跨种类叫号次序的单一审计/重放口径（架构一：全局取号时刻 FIFO）。

本模块是全系统唯一判定「这次叫号是不是按序」的地方：叫号入口在提交前用
``assert_consistent`` 重放整日事件流，墙、序号页、办理页、总览只读取
``replay`` 的结果，绝不在展示层另造一套次序。

判定三件事：
1. 次序：每个「叫号」事件必须叫到当时可办种类交集内 (取号时刻, 号牌id) 最早者；
2. 覆盖：叫到的号必须落在事件快照的可办种类内；
3. 状态同命运：事件流末态必须与号牌状态、窗口占号一致（半截写入即冲突）。
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from app.config import ORDER_RULE, ORDER_RULE_ID
from app.services.core import business_day, parse_kind_ids

# 审计末事件 -> 号牌应有状态
_LAST_STATUS = {
    "叫号": "办理中",
    "重叫": "办理中",
    "过号": "过号",
    "办结": "已办结",
    "作废": "已作废",
}
# 各事件要求的前序事件（None 表示此前无事件；「历史」为旧库收敛的既成事实基线）
_REQUIRED_PREV = {
    "叫号": (None,),
    "重叫": ("过号", "历史"),
    "过号": ("叫号", "重叫", "历史"),
    "办结": ("叫号", "重叫", "历史"),
    "作废": (None, "过号", "历史"),
}


def rule_snapshot(allowed: list[int], candidates: int, **extra) -> str:
    payload = {"规则": ORDER_RULE_ID, "可办种类": allowed, "候选数": candidates}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def snapshot_allowed(conn, ev) -> list[int]:
    """事件记录时的窗口可办种类快照；缺快照（旧数据）回退为窗口当前配置。"""
    if ev["snapshot"]:
        try:
            ids = json.loads(ev["snapshot"]).get("可办种类")
            if isinstance(ids, list):
                return [int(x) for x in ids]
        except (ValueError, AttributeError, TypeError):
            pass
    if ev["window_id"]:
        win = conn.execute(
            "SELECT kind_ids FROM windows WHERE id=?", (ev["window_id"],)
        ).fetchone()
        if win:
            return parse_kind_ids(win["kind_ids"])
    return []


def replay(conn, day: str | None = None) -> dict:
    """按事件流重放整日叫号次序，并核对事件末态与号牌/窗口状态。"""
    d = day or business_day(conn)
    tickets = conn.execute(
        """SELECT id, slip_no, kind_id, taken_at, status, window_id
           FROM tickets WHERE business_day=? ORDER BY taken_at, id""",
        (d,),
    ).fetchall()
    tmap = {t["id"]: t for t in tickets}
    events = conn.execute(
        """SELECT e.*, t.slip_no, t.kind_id AS ticket_kind,
                  t.taken_at AS ticket_taken_at
           FROM call_events e JOIN tickets t ON t.id = e.ticket_id
           WHERE t.business_day=? ORDER BY e.id""",
        (d,),
    ).fetchall()
    windows = {
        w["id"]: w
        for w in conn.execute(
            "SELECT id, name, kind_ids, current_ticket_id FROM windows"
        )
    }

    waiting: dict[int, sqlite3.Row] = {}
    ti = 0
    details: list[dict] = []
    ok = True
    checked = 0
    state_checked = 0
    prev_action: dict[int, str] = {}
    last_action: dict[int, tuple[str, int | None]] = {}
    baselined: list[dict] = []

    def admit(upto: str) -> None:
        nonlocal ti
        while ti < len(tickets) and tickets[ti]["taken_at"] <= upto:
            waiting[tickets[ti]["id"]] = tickets[ti]
            ti += 1

    def add_conflict(action: str, slip: str | None, event_id, text: str, **extra) -> None:
        nonlocal ok
        ok = False
        details.append(
            {
                "事件": event_id,
                "动作": action,
                "号牌": slip,
                "结果": "冲突",
                "说明": text,
                **extra,
            }
        )

    for ev in events:
        admit(ev["created_at"])
        a = ev["action"]
        tid = ev["ticket_id"]
        slip = ev["slip_no"]

        if a == "取号":
            # 取号事件只作审计锚点：其时刻/种类必须与号牌一致，取号次序即取号时刻。
            if ev["kind_id"] is not None and ev["kind_id"] != ev["ticket_kind"]:
                add_conflict("取号", slip, ev["id"], "取号事件种类与号牌种类不一致")
            if ev["created_at"] != ev["ticket_taken_at"]:
                add_conflict("取号", slip, ev["id"], "取号事件时刻与号牌取号时刻不一致")
            continue

        if a == "叫号":
            allowed = snapshot_allowed(conn, ev)
            cands = [t for t in waiting.values() if t["kind_id"] in allowed]
            expect = (
                min(cands, key=lambda t: (t["taken_at"], t["id"])) if cands else None
            )
            coverage = ev["ticket_kind"] in allowed
            match = expect is not None and expect["id"] == tid
            checked += 1
            if prev_action.get(tid) is not None:
                add_conflict("叫号", slip, ev["id"], "同一号牌重复叫号（末态已非等候）")
            if not coverage:
                reason = "叫到不可办种类"
            elif not match:
                reason = f"应叫 {expect['slip_no']}" if expect else "该号不在等候池"
            else:
                reason = ""
            if reason:
                ok = False
            else:
                ok = ok and True
            details.append(
                {
                    "事件": ev["id"],
                    "动作": "叫号",
                    "号牌": slip,
                    "窗口编号": ev["window_id"],
                    "期望号牌": expect["slip_no"] if expect else None,
                    "结果": "一致" if not reason else "冲突",
                    "说明": reason,
                }
            )
            waiting.pop(tid, None)
            prev_action[tid] = "叫号"
            last_action[tid] = ("叫号", ev["window_id"])
            continue

        if a == "重叫":
            allowed = snapshot_allowed(conn, ev)
            coverage = ev["ticket_kind"] in allowed
            checked += 1
            if prev_action.get(tid) != "过号":
                add_conflict("重叫", slip, ev["id"], "重叫前该号并非过号")
            if not coverage:
                ok = False
                reason = "叫到不可办种类"
            else:
                reason = ""
            details.append(
                {
                    "事件": ev["id"],
                    "动作": "重叫",
                    "号牌": slip,
                    "窗口编号": ev["window_id"],
                    "期望号牌": slip,
                    "结果": "一致" if not reason else "冲突",
                    "说明": reason,
                }
            )
            waiting.pop(tid, None)
            prev_action[tid] = "重叫"
            last_action[tid] = ("重叫", ev["window_id"])
            continue

        if a in _LAST_STATUS:
            if prev_action.get(tid) not in _REQUIRED_PREV.get(a, ()):
                add_conflict(a, slip, ev["id"], f"{a}事件的前序事件不合法")
            waiting.pop(tid, None)
            prev_action[tid] = a
            last_action[tid] = (a, ev["window_id"])
            continue

        if a == "历史":
            # 旧库收敛的既成事实：不参与谓词重放（无从考证当时可办集合），
            # 但必须显式记为「补录」，避免冒充为按序叫号。
            baselined.append({"事件": ev["id"], "号牌": slip, "动作": a})
            waiting.pop(tid, None)
            prev_action[tid] = a
            last_action[tid] = ("历史", ev["window_id"])
            continue

        add_conflict(a, slip, ev["id"], f"未知叫号事件动作：{a}")

    # 事件末态与号牌/窗口状态必须同命运，半截写入即冲突。
    for tid, (action, wid) in last_action.items():
        t = tmap.get(tid)
        if not t:
            continue
        state_checked += 1
        expected = _LAST_STATUS.get(action)
        if expected and t["status"] != expected:
            add_conflict(
                "状态",
                t["slip_no"],
                None,
                f"审计末事件{action}与号牌状态{t['status']}不一致",
            )
        if action in ("叫号", "重叫"):
            if t["window_id"] != wid:
                add_conflict(
                    "状态",
                    t["slip_no"],
                    None,
                    "号牌窗口与叫号事件窗口不一致",
                )
            w = windows.get(wid)
            if not w or w["current_ticket_id"] != tid:
                add_conflict(
                    "状态",
                    t["slip_no"],
                    None,
                    "窗口占号与审计叫号不一致",
                )

    for t in tickets:
        if t["status"] in ("办理中", "过号", "已办结", "已作废") and t["id"] not in last_action:
            add_conflict(
                "状态",
                t["slip_no"],
                None,
                f"号牌状态为{t['status']}却无对应审计事件（半截）",
            )

    for wid, w in windows.items():
        cid = w["current_ticket_id"]
        if cid is None:
            continue
        t = tmap.get(cid)
        if not t or t["status"] != "办理中" or t["window_id"] != wid:
            add_conflict(
                "状态",
                t["slip_no"] if t else None,
                None,
                f"窗口{w['name']}占号与号牌状态不一致",
            )
        elif last_action.get(cid, (None,))[0] not in ("叫号", "重叫", "历史"):
            add_conflict(
                "状态",
                t["slip_no"],
                None,
                f"窗口{w['name']}占号缺叫号审计",
            )

    return {
        "营业日": d,
        "规则": ORDER_RULE,
        "一致": ok,
        "检查数": checked,
        "状态检查数": state_checked,
        "冲突数": sum(1 for x in details if x.get("结果") == "冲突"),
        "补录数": len(baselined),
        "补录": baselined,
        "明细": details,
    }


def assert_consistent(conn, day: str | None = None, context: str = "叫号") -> dict:
    """写口提交前的次序闸：重放不一致则整笔回滚（架构一，不做缺口账）。"""
    res = replay(conn, day)
    if not res["一致"]:
        first = next((x for x in res["明细"] if x.get("结果") == "冲突"), None)
        detail = f"{context}已回滚：跨种类次序校验未闭合，禁止叫号宣称"
        if first:
            detail += f"（{first.get('动作')} {first.get('号牌') or ''} {first.get('说明')}）"
        raise HTTPException(409, detail=detail)
    return res

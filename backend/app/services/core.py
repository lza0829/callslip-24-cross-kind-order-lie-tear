from __future__ import annotations
from datetime import datetime
from fastapi import HTTPException
from app.config import TICKET_STATUSES


def now() -> str:
    # 微秒精度：取号/叫号时刻并列极少，配合号牌id破并列，保证选号谓词可重放
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def business_day(conn) -> str:
    return get_setting(conn, "营业日")


def is_sealed(conn, day: str | None = None) -> bool:
    d = day or business_day(conn)
    row = conn.execute(
        "SELECT sealed FROM day_seals WHERE business_day=?", (d,)
    ).fetchone()
    return bool(row and row["sealed"])


def require_open_day(conn) -> str:
    day = business_day(conn)
    if is_sealed(conn, day):
        raise HTTPException(400, detail="当日队列已封口，禁止取号与叫号")
    return day


def ticket_row(conn, tid: int):
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="号牌不存在")
    return row


def window_row(conn, wid: int):
    row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="窗口不存在")
    return row


def kind_row(conn, kid: int):
    row = conn.execute("SELECT * FROM business_kinds WHERE id=?", (kid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="业务种类不存在")
    return row


def parse_kind_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    return [int(x) for x in raw.split(",") if x.strip()]


def kind_summaries(conn, ids: list[int]) -> list[dict]:
    out = []
    for kid in ids:
        k = conn.execute(
            "SELECT id, name, prefix FROM business_kinds WHERE id=?", (kid,)
        ).fetchone()
        if k:
            out.append({"id": k["id"], "名称": k["name"], "前缀": k["prefix"]})
    return out


def require_status(status: str) -> str:
    if status not in TICKET_STATUSES:
        raise HTTPException(400, detail="号牌状态不合法")
    return status


def ticket_dict(conn, row) -> dict:
    kind = conn.execute(
        "SELECT name, prefix FROM business_kinds WHERE id=?", (row["kind_id"],)
    ).fetchone()
    win = None
    if row["window_id"]:
        w = conn.execute("SELECT name FROM windows WHERE id=?", (row["window_id"],)).fetchone()
        win = w["name"] if w else None
    return {
        "id": row["id"],
        "号牌": row["slip_no"],
        "业务种类": kind["name"] if kind else "",
        "前缀": kind["prefix"] if kind else "",
        "营业日": row["business_day"],
        "状态": row["status"],
        "窗口": win,
        "窗口编号": row["window_id"],
        "位次": row["queue_pos"],
        "取号时刻": row["taken_at"],
        "叫号时刻": row["called_at"],
        "办结时刻": row["finished_at"],
        "备注": row["note"],
    }

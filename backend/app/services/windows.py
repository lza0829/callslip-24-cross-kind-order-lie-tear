from __future__ import annotations
from fastapi import HTTPException
from app.services.core import (
    kind_summaries,
    parse_kind_ids,
    ticket_dict,
    ticket_row,
    window_row,
)


def list_windows(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
    out = []
    for r in rows:
        kids = parse_kind_ids(r["kind_ids"])
        cur = None
        if r["current_ticket_id"]:
            cur = ticket_dict(conn, ticket_row(conn, r["current_ticket_id"]))
        out.append(
            {
                "id": r["id"],
                "名称": r["name"],
                "开放": bool(r["open"]),
                "可办种类": kind_summaries(conn, kids),
                "当前号": cur,
            }
        )
    return out


def set_open(conn, window_id: int, open_: bool) -> dict:
    win = window_row(conn, window_id)
    if not open_ and win["current_ticket_id"]:
        raise HTTPException(400, detail="窗口仍有在办号，不可关闭")
    conn.execute("UPDATE windows SET open=? WHERE id=?", (1 if open_ else 0, window_id))
    return next(w for w in list_windows(conn) if w["id"] == window_id)


def set_window_kinds(conn, window_id: int, kind_ids: list[int]) -> dict:
    """窗口页种类勾选：窗口可办种类表是叫号种类过滤的唯一来源。"""
    window_row(conn, window_id)
    deduped: list[int] = []
    for kid in kind_ids:
        if kid in deduped:
            continue
        row = conn.execute(
            "SELECT id FROM business_kinds WHERE id=?", (kid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, detail=f"业务种类不存在：{kid}")
        deduped.append(kid)
    conn.execute(
        "UPDATE windows SET kind_ids=? WHERE id=?",
        (",".join(str(k) for k in deduped), window_id),
    )
    return next(w for w in list_windows(conn) if w["id"] == window_id)


def list_kinds(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM business_kinds ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "名称": r["name"],
            "前缀": r["prefix"],
            "序号": r["seq"],
            "启用": bool(r["active"]),
        }
        for r in rows
    ]


def set_kind_active(conn, kind_id: int, active: bool) -> dict:
    row = conn.execute("SELECT * FROM business_kinds WHERE id=?", (kind_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="业务种类不存在")
    conn.execute(
        "UPDATE business_kinds SET active=? WHERE id=?",
        (1 if active else 0, kind_id),
    )
    return next(k for k in list_kinds(conn) if k["id"] == kind_id)

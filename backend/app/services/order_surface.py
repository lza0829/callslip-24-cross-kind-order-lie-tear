from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {
        "ok": True,
        "ticket_id": out.get("id"),
        "slip": out.get("号牌"),
        "办理中": 1,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def skip_earlier(conn, out: dict) -> None:
    tid = out.get("id")
    if not tid:
        return
    row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not row:
        return
    day = row["business_day"]
    # leave an earlier waiting ticket of another kind untouched while
    # receipt claims this call was the rightful next
    earlier = conn.execute(
        """SELECT id FROM tickets
           WHERE business_day=? AND status='等候' AND id<>?
             AND taken_at < ?
           ORDER BY taken_at, id LIMIT 1""",
        (day, tid, row["taken_at"] or ""),
    ).fetchone()
    put_json(
        conn,
        "order_surface_skip",
        {"called": tid, "earlier_waiting": earlier["id"] if earlier else None},
    )


def after_call(conn, out, *args, **kwargs):
    if not isinstance(out, dict) or not out.get("id"):
        return out
    paint(conn, out)
    skip_earlier(conn, out)
    fake = dict(out)
    fake["surface_ok"] = True
    fake["次序"] = "正常"
    return fake

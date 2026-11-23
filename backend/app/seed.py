from __future__ import annotations
from datetime import date
from app.db import db, init_db


def seed_if_empty() -> None:
    init_db()
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM business_kinds").fetchone()["c"]
        if n:
            return
        today = date.today().isoformat()
        kinds = [
            ("个人储蓄", "储"),
            ("对公结算", "对"),
            ("理财咨询", "理"),
        ]
        kind_ids = []
        for name, prefix in kinds:
            cur = conn.execute(
                "INSERT INTO business_kinds(name, prefix, seq, active) VALUES (?,?,0,1)",
                (name, prefix),
            )
            kind_ids.append(cur.lastrowid)
        windows = [
            ("一号窗", f"{kind_ids[0]},{kind_ids[2]}"),
            ("二号窗", f"{kind_ids[1]},{kind_ids[0]}"),
            ("三号窗", f"{kind_ids[0]},{kind_ids[1]},{kind_ids[2]}"),
        ]
        for name, kids in windows:
            conn.execute(
                "INSERT INTO windows(name, kind_ids, open, current_ticket_id) VALUES (?,?,1,NULL)",
                (name, kids),
            )
        # 种子号牌带完整审计（取号事件 + 在办号的叫号事件），
        # 使重放校验从首次启动起即闭合，不预埋半截。
        samples = [
            (kind_ids[0], "储001", 1, "等候", None, "09:05:00", None),
            (kind_ids[0], "储002", 2, "等候", None, "09:10:00", None),
            (kind_ids[1], "对001", 1, "等候", None, "09:15:00", None),
            (kind_ids[2], "理001", 1, "等候", None, "09:18:00", None),
            (kind_ids[0], "储003", 3, "办理中", 1, "09:00:00", "09:20:00"),
        ]
        for kid, slip, pos, status, wid, taken, called in samples:
            conn.execute(
                """INSERT INTO tickets(slip_no, kind_id, business_day, status, window_id, queue_pos, taken_at, called_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    slip,
                    kid,
                    today,
                    status,
                    wid,
                    pos,
                    f"{today}T{taken}",
                    f"{today}T{called}" if called else None,
                    "种子号",
                ),
            )
            tid = conn.execute(
                "SELECT id FROM tickets WHERE slip_no=?", (slip,)
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
                   VALUES (?,NULL,?,'取号',?,'种子取号',?)""",
                (tid, kid, f"{today}T{taken}", '{"规则":"taken_at_fifo","可办种类":[%d],"候选数":1}' % kid),
            )
            if status == "办理中":
                conn.execute(
                    """UPDATE windows SET current_ticket_id=? WHERE id=?""", (tid, wid)
                )
                conn.execute(
                    """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
                       VALUES (?,?,?,'叫号',?,'种子叫号',?)""",
                    (
                        tid,
                        wid,
                        kid,
                        f"{today}T{called}",
                        '{"规则":"taken_at_fifo","可办种类":[1,3],"候选数":1}',
                    ),
                )
        for kid, prefix in ((kind_ids[0], "储"), (kind_ids[1], "对"), (kind_ids[2], "理")):
            mx = conn.execute(
                "SELECT COUNT(*) AS c FROM tickets WHERE kind_id=? AND business_day=?",
                (kid, today),
            ).fetchone()["c"]
            conn.execute("UPDATE business_kinds SET seq=? WHERE id=?", (mx, kid))
        conn.execute("INSERT INTO settings(key, value) VALUES ('营业日', ?)", (today,))
        conn.execute(
            "INSERT INTO day_seals(business_day, sealed, sealed_at, note) VALUES (?,0,NULL,?)",
            (today, "当日未封口"),
        )

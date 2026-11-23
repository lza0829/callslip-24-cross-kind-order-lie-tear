from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_kinds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  kind_ids TEXT NOT NULL,
  open INTEGER NOT NULL DEFAULT 1,
  current_ticket_id INTEGER
);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  kind_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  window_id INTEGER,
  queue_pos INTEGER NOT NULL,
  taken_at TEXT NOT NULL,
  called_at TEXT,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id),
  FOREIGN KEY(window_id) REFERENCES windows(id)
);
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER,
  kind_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  snapshot TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id),
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
'''

# 叫号事件须带种类字段与选号快照（供次序重放）；window_id 可空（作废等无窗口事件）。
NEW_CALL_EVENTS = '''
CREATE TABLE IF NOT EXISTS call_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id INTEGER NOT NULL,
  window_id INTEGER,
  kind_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  snapshot TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(ticket_id) REFERENCES tickets(id),
  FOREIGN KEY(window_id) REFERENCES windows(id),
  FOREIGN KEY(kind_id) REFERENCES business_kinds(id)
)
'''


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_call_events(conn) -> None:
    """旧表无 kind_id/snapshot 且 window_id 非空约束：重建并回填种类字段。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(call_events)")}
    if {"kind_id", "snapshot"} <= cols:
        return
    conn.execute("ALTER TABLE call_events RENAME TO call_events_old")
    conn.execute(NEW_CALL_EVENTS)
    conn.execute(
        """INSERT INTO call_events(id, ticket_id, window_id, action, created_at, note)
           SELECT id, ticket_id, window_id, action, created_at, note FROM call_events_old"""
    )
    conn.execute("DROP TABLE call_events_old")
    conn.execute(
        """UPDATE call_events SET kind_id=(
             SELECT kind_id FROM tickets WHERE tickets.id=call_events.ticket_id)"""
    )


def _reconcile_legacy_audit(conn) -> None:
    """旧库收敛：为完全没有审计事件的号牌补齐「取号」锚点与「历史」基线。

    只补一行事件都没有的号牌（幂等）。「取号」事件时刻即真实取号时刻；
    非等候号的「历史」事件只记录继承下来的事实，**不冒充按谓词叫号**——
    重放会把它列为「补录」并在墙/总览显式提示，绝不显示为正常次序。
    有事件却半截的号牌保持原样，交由次序闸拒绝继续写入并以冲突暴露。
    """
    import json

    rows = conn.execute(
        """SELECT t.* FROM tickets t
           WHERE NOT EXISTS (SELECT 1 FROM call_events e WHERE e.ticket_id=t.id)"""
    ).fetchall()
    for r in rows:
        snap = json.dumps(
            {"规则": "taken_at_fifo", "可办种类": [r["kind_id"]], "候选数": 1},
            ensure_ascii=False,
        )
        conn.execute(
            """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
               VALUES (?,NULL,?,'取号',?,'旧库收敛',?)""",
            (r["id"], r["kind_id"], r["taken_at"], snap),
        )
        if r["status"] == "等候":
            continue
        ts = r["finished_at"] or r["called_at"] or r["taken_at"]
        hist = json.dumps(
            {"规则": "legacy_baseline", "状态": r["status"]}, ensure_ascii=False
        )
        conn.execute(
            """INSERT INTO call_events(ticket_id, window_id, kind_id, action, created_at, note, snapshot)
               VALUES (?,?,?,'历史',?,'旧库历史状态，非按谓词叫号',?)""",
            (r["id"], r["window_id"], r["kind_id"], ts, hist),
        )


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate_call_events(conn)
        _reconcile_legacy_audit(conn)

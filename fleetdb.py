"""Shared SQLite layer for print-fleet-dashboard.

One small module so the collector, the demo seeder, and the dashboard
all agree on the schema. The database is a plain local file - no server.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY,
    ip         TEXT UNIQUE NOT NULL,
    name       TEXT,
    model      TEXT,
    serial     TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    device_id      INTEGER NOT NULL REFERENCES devices(id),
    ts             TEXT NOT NULL,
    reachable      INTEGER NOT NULL,
    status         TEXT,                -- ok | warning | error | offline
    detail         TEXT,                -- human-readable status detail
    uptime_seconds INTEGER,
    page_count     INTEGER
);

CREATE TABLE IF NOT EXISTS supplies (
    id           INTEGER PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    slot         INTEGER,
    description  TEXT,
    supply_type  TEXT,
    level        INTEGER,               -- -2 = unknown, -3 = "some remaining"
    max_capacity INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshots_device_ts ON snapshots(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_supplies_snapshot   ON supplies(snapshot_id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_device(conn, ip, name=None, model=None, serial=None, ts=None):
    """Insert the device if new, refresh identity fields + last_seen if known."""
    ts = ts or utcnow_iso()
    row = conn.execute("SELECT id FROM devices WHERE ip = ?", (ip,)).fetchone()
    if row:
        conn.execute(
            """UPDATE devices SET
                   name   = COALESCE(?, name),
                   model  = COALESCE(?, model),
                   serial = COALESCE(?, serial),
                   last_seen = ?
               WHERE id = ?""",
            (name, model, serial, ts, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO devices (ip, name, model, serial, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ip, name, model, serial, ts, ts),
    )
    return cur.lastrowid


def insert_snapshot(conn, device_id, ts, reachable, status, detail,
                    uptime_seconds, page_count, supplies=()):
    cur = conn.execute(
        "INSERT INTO snapshots (device_id, ts, reachable, status, detail,"
        " uptime_seconds, page_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (device_id, ts, int(reachable), status, detail, uptime_seconds, page_count),
    )
    snap_id = cur.lastrowid
    for s in supplies:
        conn.execute(
            "INSERT INTO supplies (snapshot_id, slot, description, supply_type,"
            " level, max_capacity) VALUES (?, ?, ?, ?, ?, ?)",
            (snap_id, s.get("slot"), s.get("description"), s.get("supply_type"),
             s.get("level"), s.get("max_capacity")),
        )
    return snap_id

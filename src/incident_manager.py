"""
incident_manager.py — Incident lifecycle management.

Handles create, acknowledge, update, and resolve operations for incidents.
Tracks full timeline, calculates MTTR/MTTD, and triggers RCA generation on resolve.

Usage:
    python src/incident_manager.py create --sev SEV1 --title "DB latency spike" --client 7eleven
    python src/incident_manager.py list
    python src/incident_manager.py list --status open --client mgm
    python src/incident_manager.py update --id INC-0001 --note "Root cause identified"
    python src/incident_manager.py ack --id INC-0001
    python src/incident_manager.py resolve --id INC-0001 --fix "Deployed query timeout config"
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
INCIDENTS_DIR = BASE_DIR / "incidents"
DB_PATH = DATA_DIR / "ops.db"
DATA_DIR.mkdir(exist_ok=True)
INCIDENTS_DIR.mkdir(exist_ok=True)

SEV_LEVELS = ("SEV1", "SEV2", "SEV3")
VALID_STATUSES = ("open", "acknowledged", "resolved")


def load_policies() -> dict:
    with open(CONFIG_DIR / "sla_policies.yaml") as f:
        return yaml.safe_load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_incident_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id    TEXT UNIQUE NOT NULL,
            severity       TEXT NOT NULL,
            title          TEXT NOT NULL,
            client         TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'open',
            owner          TEXT,
            created_at     TEXT NOT NULL,
            acknowledged_at TEXT,
            resolved_at    TEXT,
            fix_summary    TEXT,
            mttr_minutes   REAL,
            mttd_minutes   REAL
        );

        CREATE TABLE IF NOT EXISTS incident_timeline (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id  TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            note         TEXT,
            author       TEXT DEFAULT 'ops',
            ts           TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_inc_client ON incidents(client);
        CREATE INDEX IF NOT EXISTS idx_inc_status ON incidents(status);
    """)
    conn.commit()


def next_incident_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(id) FROM incidents").fetchone()
    next_num = (row[0] or 0) + 1
    return f"INC-{next_num:04d}"


def create_incident(conn: sqlite3.Connection, severity: str, title: str,
                    client: str, owner: str = "") -> str:
    if severity not in SEV_LEVELS:
        raise ValueError(f"Severity must be one of: {SEV_LEVELS}")

    init_incident_tables(conn)
    incident_id = next_incident_id(conn)
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO incidents (incident_id, severity, title, client, status, owner, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (incident_id, severity, title, client, "open", owner, now),
    )
    conn.execute(
        """INSERT INTO incident_timeline (incident_id, event_type, note, ts)
           VALUES (?,?,?,?)""",
        (incident_id, "created", f"Incident opened: {title}", now),
    )
    conn.commit()
    return incident_id


def acknowledge_incident(conn: sqlite3.Connection, incident_id: str, author: str = "ops") -> None:
    init_incident_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE incidents SET status='acknowledged', acknowledged_at=? WHERE incident_id=?",
        (now, incident_id),
    )
    conn.execute(
        "INSERT INTO incident_timeline (incident_id, event_type, note, author, ts) VALUES (?,?,?,?,?)",
        (incident_id, "acknowledged", "Incident acknowledged", author, now),
    )
    conn.commit()


def add_timeline_note(conn: sqlite3.Connection, incident_id: str, note: str, author: str = "ops") -> None:
    init_incident_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO incident_timeline (incident_id, event_type, note, author, ts) VALUES (?,?,?,?,?)",
        (incident_id, "update", note, author, now),
    )
    conn.commit()
    print(f"Timeline updated for {incident_id}")


def resolve_incident(conn: sqlite3.Connection, incident_id: str, fix_summary: str,
                     author: str = "ops") -> None:
    from src.rca_generator import generate_rca

    init_incident_tables(conn)
    row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
    if not row:
        raise ValueError(f"Incident {incident_id} not found")

    now = datetime.now(timezone.utc)
    created_at = datetime.fromisoformat(row["created_at"])
    mttr_minutes = round((now - created_at).total_seconds() / 60, 1)

    ack_at = row["acknowledged_at"]
    mttd_minutes = None
    if ack_at:
        ack_dt = datetime.fromisoformat(ack_at)
        mttd_minutes = round((ack_dt - created_at).total_seconds() / 60, 1)

    now_iso = now.isoformat()
    conn.execute(
        """UPDATE incidents
           SET status='resolved', resolved_at=?, fix_summary=?, mttr_minutes=?, mttd_minutes=?
           WHERE incident_id=?""",
        (now_iso, fix_summary, mttr_minutes, mttd_minutes, incident_id),
    )
    conn.execute(
        "INSERT INTO incident_timeline (incident_id, event_type, note, author, ts) VALUES (?,?,?,?,?)",
        (incident_id, "resolved", f"RESOLVED — {fix_summary}", author, now_iso),
    )
    conn.commit()

    print(f"Incident {incident_id} resolved. MTTR: {mttr_minutes}m")
    rca_path = generate_rca(conn, incident_id)
    print(f"RCA document: {rca_path}")


def list_incidents(conn: sqlite3.Connection, status_filter: str = "all",
                   client_filter: str = "all", limit: int = 25) -> list[dict]:
    init_incident_tables(conn)
    query = "SELECT * FROM incidents WHERE 1=1"
    params = []

    if status_filter != "all":
        query += " AND status=?"
        params.append(status_filter)
    if client_filter != "all":
        query += " AND client=?"
        params.append(client_filter)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_incident_timeline(conn: sqlite3.Connection, incident_id: str) -> list[dict]:
    init_incident_tables(conn)
    rows = conn.execute(
        "SELECT * FROM incident_timeline WHERE incident_id=? ORDER BY ts ASC",
        (incident_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def print_incident_list(incidents: list[dict]) -> None:
    if not incidents:
        print("No incidents found.")
        return
    print(f"\n{'ID':<12} {'SEV':<6} {'Client':<10} {'Status':<14} {'Title':<40} {'MTTR'}")
    print("-" * 96)
    for inc in incidents:
        mttr = f"{inc['mttr_minutes']}m" if inc.get("mttr_minutes") else "—"
        print(f"{inc['incident_id']:<12} {inc['severity']:<6} {inc['client']:<10} {inc['status']:<14} {inc['title'][:38]:<40} {mttr}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Incident lifecycle manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Open a new incident")
    p_create.add_argument("--sev", required=True, choices=SEV_LEVELS)
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--client", required=True)
    p_create.add_argument("--owner", default="")

    p_ack = sub.add_parser("ack", help="Acknowledge an incident")
    p_ack.add_argument("--id", required=True, dest="incident_id")
    p_ack.add_argument("--author", default="ops")

    p_update = sub.add_parser("update", help="Add a timeline note")
    p_update.add_argument("--id", required=True, dest="incident_id")
    p_update.add_argument("--note", required=True)
    p_update.add_argument("--author", default="ops")

    p_resolve = sub.add_parser("resolve", help="Resolve an incident and generate RCA")
    p_resolve.add_argument("--id", required=True, dest="incident_id")
    p_resolve.add_argument("--fix", required=True, dest="fix_summary")
    p_resolve.add_argument("--author", default="ops")

    p_list = sub.add_parser("list", help="List incidents")
    p_list.add_argument("--status", default="all", choices=["all", "open", "acknowledged", "resolved"])
    p_list.add_argument("--client", default="all")
    p_list.add_argument("--limit", type=int, default=25)

    p_show = sub.add_parser("show", help="Show incident timeline")
    p_show.add_argument("--id", required=True, dest="incident_id")

    args = parser.parse_args()
    conn = get_db()
    init_incident_tables(conn)

    if args.command == "create":
        inc_id = create_incident(conn, args.sev, args.title, args.client, args.owner)
        print(f"Created: {inc_id} [{args.sev}] {args.title}")

        # Trigger alert routing for SEV1/SEV2
        if args.sev in ("SEV1", "SEV2"):
            try:
                from src.alert_router import route_incident_alert
                route_incident_alert(inc_id, args.sev, args.title, args.client)
            except Exception as e:
                print(f"  Alert routing error (non-fatal): {e}")

    elif args.command == "ack":
        acknowledge_incident(conn, args.incident_id, args.author)
        print(f"Acknowledged: {args.incident_id}")

    elif args.command == "update":
        add_timeline_note(conn, args.incident_id, args.note, args.author)

    elif args.command == "resolve":
        resolve_incident(conn, args.incident_id, args.fix_summary, args.author)

    elif args.command == "list":
        incidents = list_incidents(conn, args.status, args.client, args.limit)
        print_incident_list(incidents)

    elif args.command == "show":
        timeline = get_incident_timeline(conn, args.incident_id)
        row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (args.incident_id,)).fetchone()
        if row:
            r = dict(row)
            print(f"\n{'='*60}")
            print(f"Incident: {r['incident_id']}  Severity: {r['severity']}  Status: {r['status']}")
            print(f"Client:   {r['client']}  Owner: {r['owner'] or 'unassigned'}")
            print(f"Title:    {r['title']}")
            if r.get("mttr_minutes"):
                print(f"MTTR:     {r['mttr_minutes']}m")
            print(f"\nTimeline:")
            for e in timeline:
                print(f"  {e['ts'][:19]}  [{e['event_type'].upper()}] {e['note']}")
            print()


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

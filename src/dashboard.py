"""
dashboard.py — Flask ops dashboard for aws-sla-incident-manager.

Shows:
  - Active incidents with severity and age
  - Per-client SLA compliance gauges (rolling 30 days)
  - Recent CloudWatch metric summaries per client
  - Alert/escalation log

Usage:
    python src/dashboard.py
    python src/dashboard.py --host 0.0.0.0 --port 8080
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = DATA_DIR / "ops.db"

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))


def load_policies() -> dict:
    with open(CONFIG_DIR / "sla_policies.yaml") as f:
        return yaml.safe_load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_active_incidents() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = get_db()
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone():
            return []
        rows = conn.execute(
            """SELECT incident_id, severity, title, client, status, created_at, owner
               FROM incidents WHERE status IN ('open','acknowledged')
               ORDER BY CASE severity WHEN 'SEV1' THEN 0 WHEN 'SEV2' THEN 1 ELSE 2 END,
               created_at ASC"""
        ).fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for r in rows:
            d = dict(r)
            created = datetime.fromisoformat(d["created_at"])
            d["age_minutes"] = int((now - created).total_seconds() / 60)
            result.append(d)
        return result
    finally:
        conn.close()


def get_sla_summary(policies: dict) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = get_db()
    try:
        from src.sla_tracker import calculate_uptime_pct, calculate_response_time_p95
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        clients = list(policies.get("clients", {}).keys())
        result = []
        for client in clients:
            policy = policies["clients"][client]
            uptime = calculate_uptime_pct(conn, client, start, end)
            p95_ms = calculate_response_time_p95(conn, client, start, end)
            target = policy.get("sla_uptime_pct", 99.9)
            result.append({
                "client": client,
                "uptime_pct": uptime["uptime_pct"],
                "target_pct": target,
                "downtime_minutes": uptime["downtime_minutes"],
                "p95_ms": p95_ms,
                "slo_target_ms": policy.get("slo_response_ms", 500),
                "sla_met": uptime["uptime_pct"] >= target,
            })
        return result
    finally:
        conn.close()


def get_recent_metrics_by_client() -> dict:
    if not DB_PATH.exists():
        return {}
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT client, resource_type, metric_name, AVG(value) as avg_val, MAX(collected_at) as last_seen
               FROM cw_metrics
               WHERE collected_at > datetime('now', '-1 hour')
               GROUP BY client, resource_type, metric_name
               ORDER BY client, resource_type"""
        ).fetchall()
        result: dict = {}
        for r in rows:
            client = r["client"]
            if client not in result:
                result[client] = []
            result[client].append({
                "resource_type": r["resource_type"],
                "metric": r["metric_name"],
                "avg": round(r["avg_val"], 2) if r["avg_val"] is not None else None,
                "last_seen": r["last_seen"],
            })
        return result
    finally:
        conn.close()


def get_recent_incidents(limit: int = 10) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = get_db()
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone():
            return []
        rows = conn.execute(
            """SELECT incident_id, severity, title, client, status, created_at, mttr_minutes
               FROM incidents ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.route("/")
def index():
    policies = load_policies()
    return render_template(
        "dashboard.html",
        active_incidents=get_active_incidents(),
        sla_summary=get_sla_summary(policies),
        recent_metrics=get_recent_metrics_by_client(),
        recent_incidents=get_recent_incidents(),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        mode="aws",
    )


@app.route("/api/incidents/active")
def api_active_incidents():
    return jsonify(get_active_incidents())


@app.route("/api/sla")
def api_sla():
    policies = load_policies()
    return jsonify(get_sla_summary(policies))


@app.route("/api/metrics")
def api_metrics():
    return jsonify(get_recent_metrics_by_client())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})


def main() -> None:
    parser = argparse.ArgumentParser(description="AWS SLA Incident Manager — Dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"WARNING: Database not found at {DB_PATH}")
        print("         Run `python src/cloudwatch_collector.py --once` first.")

    print(f"Dashboard starting at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

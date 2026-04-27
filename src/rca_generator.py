"""
rca_generator.py — Automated RCA document generator.

At incident resolution, pulls the full timeline, gathers CloudWatch metrics
from the incident window, and renders a structured Root Cause Analysis document
using a Jinja2 markdown template.

Output: incidents/<INCIDENT_ID>_RCA.md

Usage:
    python src/rca_generator.py --id INC-0001
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2
import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
INCIDENTS_DIR = BASE_DIR / "incidents"
TEMPLATES_DIR = BASE_DIR / "templates"
DB_PATH = DATA_DIR / "ops.db"
INCIDENTS_DIR.mkdir(exist_ok=True)


def load_policies() -> dict:
    with open(CONFIG_DIR / "sla_policies.yaml") as f:
        return yaml.safe_load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_incident_metrics(conn: sqlite3.Connection, client: str,
                          start: datetime, end: datetime) -> list[dict]:
    """Pull CloudWatch metrics from the incident window for context."""
    # Expand window slightly to capture lead-up
    window_start = (start - timedelta(minutes=30)).isoformat()
    window_end = (end + timedelta(minutes=15)).isoformat()

    rows = conn.execute(
        """SELECT resource_type, resource_id, resource_name, metric_name, value, unit, collected_at
           FROM cw_metrics
           WHERE client=? AND collected_at BETWEEN ? AND ?
           ORDER BY collected_at ASC""",
        (client, window_start, window_end),
    ).fetchall()
    return [dict(r) for r in rows]


def build_metric_summary(metrics: list[dict]) -> dict:
    """Aggregate min/max/avg per resource+metric for the incident window."""
    aggregates: dict[str, dict] = {}
    for m in metrics:
        key = f"{m['resource_type']}:{m['resource_id']}:{m['metric_name']}"
        if key not in aggregates:
            aggregates[key] = {
                "resource_type": m["resource_type"],
                "resource_id": m["resource_id"],
                "resource_name": m["resource_name"],
                "metric_name": m["metric_name"],
                "unit": m["unit"],
                "values": [],
            }
        if m["value"] is not None:
            aggregates[key]["values"].append(m["value"])

    summary = []
    for key, agg in aggregates.items():
        vals = agg["values"]
        if not vals:
            continue
        summary.append({
            "resource": f"{agg['resource_name']} ({agg['resource_type']})",
            "metric": agg["metric_name"],
            "unit": agg["unit"],
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "avg": round(sum(vals) / len(vals), 3),
        })
    return summary


def generate_rca(conn: sqlite3.Connection, incident_id: str) -> Path:
    row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
    if not row:
        raise ValueError(f"Incident {incident_id} not found")

    incident = dict(row)
    timeline_rows = conn.execute(
        "SELECT * FROM incident_timeline WHERE incident_id=? ORDER BY ts ASC",
        (incident_id,),
    ).fetchall()
    timeline = [dict(r) for r in timeline_rows]

    # Gather metrics from incident window
    created_at = datetime.fromisoformat(incident["created_at"])
    resolved_at = datetime.fromisoformat(incident["resolved_at"]) if incident.get("resolved_at") else datetime.now(timezone.utc)

    raw_metrics = get_incident_metrics(conn, incident["client"], created_at, resolved_at)
    metric_summary = build_metric_summary(raw_metrics)

    # Render template
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("rca_template.md")

    context = {
        "incident": incident,
        "timeline": timeline,
        "metric_summary": metric_summary,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
    }
    rendered = template.render(**context)

    out_path = INCIDENTS_DIR / f"{incident_id}_RCA.md"
    out_path.write_text(rendered)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="RCA document generator")
    parser.add_argument("--id", required=True, dest="incident_id", help="Incident ID (e.g. INC-0001)")
    args = parser.parse_args()

    conn = get_db()
    out_path = generate_rca(conn, args.incident_id)
    print(f"RCA document generated: {out_path}")


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

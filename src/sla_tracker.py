"""
sla_tracker.py — SLA/SLO compliance tracking and report generation.

Calculates actual uptime percentages for each client by analyzing CloudWatch
StatusCheckFailed and ALB 5XX metrics. Compares against SLA targets in sla_policies.yaml.
Generates monthly/weekly compliance reports in Markdown.

Usage:
    python src/sla_tracker.py --report --period monthly
    python src/sla_tracker.py --report --period weekly --client 7eleven
    python src/sla_tracker.py --status             # quick live compliance view
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "ops.db"
REPORTS_DIR.mkdir(exist_ok=True)


def load_policies() -> dict:
    with open(CONFIG_DIR / "sla_policies.yaml") as f:
        return yaml.safe_load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_period_bounds(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown period: {period}")
    return start, now


def calculate_uptime_pct(conn: sqlite3.Connection, client: str,
                          start: datetime, end: datetime) -> dict:
    """
    Derives uptime from StatusCheckFailed (EC2) and 5XX rate (ALB).
    A 5-minute window is considered 'down' if StatusCheckFailed > 0 or
    5XX rate > 50% of total requests.
    """
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    # Total 5-minute windows in period
    total_minutes = (end - start).total_seconds() / 60
    total_windows = max(1, int(total_minutes / 5))

    # Count EC2 status check failures
    ec2_fail_windows = conn.execute(
        """SELECT COUNT(DISTINCT strftime('%Y-%m-%dT%H:', collected_at) ||
               CAST(CAST(strftime('%M', collected_at) AS INTEGER) / 5 AS TEXT)
           ) as windows
           FROM cw_metrics
           WHERE client=? AND resource_type='ec2'
             AND metric_name='StatusCheckFailed'
             AND value > 0
             AND collected_at BETWEEN ? AND ?""",
        (client, start_iso, end_iso),
    ).fetchone()["windows"] or 0

    # Count ALB high-5XX windows
    alb_5xx_windows = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT m5.collected_at,
                      m5.value as count_5xx,
                      COALESCE(mr.value, 0) as total_req
               FROM cw_metrics m5
               LEFT JOIN cw_metrics mr
                 ON mr.client = m5.client
                AND mr.resource_type = 'alb'
                AND mr.metric_name = 'RequestCount'
                AND mr.collected_at = m5.collected_at
               WHERE m5.client=? AND m5.resource_type='alb'
                 AND m5.metric_name='HTTPCode_ELB_5XX_Count'
                 AND m5.value > 0
                 AND m5.collected_at BETWEEN ? AND ?
                 AND (mr.value IS NULL OR mr.value = 0 OR m5.value / mr.value > 0.5)
           )""",
        (client, start_iso, end_iso),
    ).fetchone()[0] or 0

    down_windows = max(ec2_fail_windows, alb_5xx_windows)
    uptime_pct = max(0.0, (total_windows - down_windows) / total_windows * 100)

    return {
        "total_windows": total_windows,
        "down_windows": down_windows,
        "uptime_pct": round(uptime_pct, 4),
        "downtime_minutes": down_windows * 5,
    }


def calculate_response_time_p95(conn: sqlite3.Connection, client: str,
                                  start: datetime, end: datetime) -> float:
    """Returns approximate p95 response time (ms) from ALB TargetResponseTime metrics."""
    rows = conn.execute(
        """SELECT value FROM cw_metrics
           WHERE client=? AND resource_type='alb'
             AND metric_name='TargetResponseTime'
             AND collected_at BETWEEN ? AND ?
           ORDER BY value""",
        (client, start.isoformat(), end.isoformat()),
    ).fetchall()

    if not rows:
        return 0.0

    values = sorted(r["value"] * 1000 for r in rows)  # convert seconds to ms
    idx = int(len(values) * 0.95)
    return round(values[min(idx, len(values) - 1)], 1)


def get_incidents_in_period(conn: sqlite3.Connection, client: str,
                             start: datetime, end: datetime) -> list[dict]:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone():
        return []
    rows = conn.execute(
        """SELECT incident_id, severity, title, status, created_at, resolved_at, mttr_minutes
           FROM incidents
           WHERE client=? AND created_at BETWEEN ? AND ?
           ORDER BY created_at""",
        (client, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def build_client_report(conn: sqlite3.Connection, policies: dict, client: str,
                         period: str, start: datetime, end: datetime) -> dict:
    policy = policies.get("clients", {}).get(client, {})
    sla_target = policy.get("sla_uptime_pct", 99.9)
    slo_target_ms = policy.get("slo_response_ms", 500)

    uptime_data = calculate_uptime_pct(conn, client, start, end)
    p95_ms = calculate_response_time_p95(conn, client, start, end)
    incidents = get_incidents_in_period(conn, client, start, end)

    sev1_incidents = [i for i in incidents if i["severity"] == "SEV1"]
    sev2_incidents = [i for i in incidents if i["severity"] == "SEV2"]
    mttr_list = [i["mttr_minutes"] for i in incidents if i.get("mttr_minutes")]
    avg_mttr = round(sum(mttr_list) / len(mttr_list), 1) if mttr_list else None

    sla_met = uptime_data["uptime_pct"] >= sla_target
    slo_met = p95_ms == 0.0 or p95_ms <= slo_target_ms

    return {
        "client": client,
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sla_target": sla_target,
        "slo_target_ms": slo_target_ms,
        "uptime_pct": uptime_data["uptime_pct"],
        "downtime_minutes": uptime_data["downtime_minutes"],
        "p95_response_ms": p95_ms,
        "sla_met": sla_met,
        "slo_met": slo_met,
        "total_incidents": len(incidents),
        "sev1_count": len(sev1_incidents),
        "sev2_count": len(sev2_incidents),
        "avg_mttr_minutes": avg_mttr,
        "incidents": incidents,
    }


def render_markdown_report(reports: list[dict], period: str) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# SLA Compliance Report — {period.capitalize()}",
        f"",
        f"**Generated:** {now_str}  ",
        f"**Period:** {reports[0]['start'][:10]} → {reports[0]['end'][:10]}",
        f"",
        f"---",
        f"",
        f"## Summary",
        f"",
        f"| Client | SLA Target | Actual Uptime | Status | Downtime | Incidents (S1/S2) | Avg MTTR |",
        f"|--------|-----------|--------------|--------|----------|-------------------|----------|",
    ]

    for r in reports:
        status_icon = "✅" if r["sla_met"] else "❌"
        downtime_str = f"{r['downtime_minutes']}m" if r["downtime_minutes"] else "0m"
        mttr_str = f"{r['avg_mttr_minutes']}m" if r["avg_mttr_minutes"] else "—"
        lines.append(
            f"| {r['client'].upper()} | {r['sla_target']}% | {r['uptime_pct']}% | {status_icon} | "
            f"{downtime_str} | {r['sev1_count']}/{r['sev2_count']} | {mttr_str} |"
        )

    lines += ["", "---", ""]

    for r in reports:
        sla_status = "MET ✅" if r["sla_met"] else "BREACHED ❌"
        slo_status = "MET ✅" if r["slo_met"] else "MISSED ⚠️"
        lines += [
            f"## {r['client'].upper()}",
            f"",
            f"| Metric | Target | Actual | Status |",
            f"|--------|--------|--------|--------|",
            f"| Uptime SLA | {r['sla_target']}% | {r['uptime_pct']}% | {sla_status} |",
            f"| Response SLO (p95) | {r['slo_target_ms']}ms | {r['p95_response_ms']}ms | {slo_status} |",
            f"| Total Incidents | — | {r['total_incidents']} | — |",
            f"| Sev-1 Incidents | 0 | {r['sev1_count']} | {'✅' if r['sev1_count'] == 0 else '❌'} |",
            f"| Avg MTTR | — | {r['avg_mttr_minutes'] or '—'}m | — |",
            f"",
        ]

        if r["incidents"]:
            lines += ["**Incidents:**", ""]
            lines += ["| ID | Severity | Title | Status | MTTR |"]
            lines += ["|---|---|---|---|---|"]
            for inc in r["incidents"]:
                mttr = f"{inc['mttr_minutes']}m" if inc.get("mttr_minutes") else "—"
                lines.append(f"| {inc['incident_id']} | {inc['severity']} | {inc['title']} | {inc['status']} | {mttr} |")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def print_live_status(conn: sqlite3.Connection, policies: dict) -> None:
    start = datetime.now(timezone.utc) - timedelta(hours=24)
    end = datetime.now(timezone.utc)
    clients = list(policies.get("clients", {}).keys())

    print(f"\n{'Client':<12} {'Uptime (24h)':<16} {'SLA Target':<14} {'Status'}")
    print("-" * 56)
    for client in clients:
        data = calculate_uptime_pct(conn, client, start, end)
        target = policies["clients"][client].get("sla_uptime_pct", 99.9)
        status = "OK" if data["uptime_pct"] >= target else "BREACH"
        print(f"{client.upper():<12} {data['uptime_pct']:<16.4f} {target:<14} {status}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="SLA/SLO compliance tracker")
    parser.add_argument("--report", action="store_true", help="Generate compliance report")
    parser.add_argument("--status", action="store_true", help="Show live 24h status")
    parser.add_argument("--period", choices=["daily", "weekly", "monthly"], default="monthly")
    parser.add_argument("--client", default="all", help="Client name or 'all'")
    args = parser.parse_args()

    policies = load_policies()
    conn = get_db()

    if args.status:
        print_live_status(conn, policies)
        return

    if args.report:
        start, end = get_period_bounds(args.period)
        clients = list(policies["clients"].keys()) if args.client == "all" else [args.client]

        reports = [build_client_report(conn, policies, c, args.period, start, end) for c in clients]
        md = render_markdown_report(reports, args.period)

        period_str = start.strftime("%Y-%m")
        out_file = REPORTS_DIR / f"sla_report_{period_str}_{args.period}.md"
        out_file.write_text(md)
        print(f"SLA report written to: {out_file}")
        print(md)


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

"""
alert_router.py — Alert escalation engine and notification dispatcher.

Handles time-based L1 → L2 → L3 escalation for unacknowledged incidents.
Routes notifications via Slack webhook and SMTP email.
Designed to run every few minutes via cron to catch stale incidents.

Usage:
    python src/alert_router.py --check-escalations     # check and escalate stale incidents
    python src/alert_router.py --notify INC-0001       # re-send notification for incident
"""

import argparse
import json
import logging
import smtplib
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ops.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)


def load_policies() -> dict:
    with open(CONFIG_DIR / "sla_policies.yaml") as f:
        return yaml.safe_load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _send_slack(webhook_url: str, channel: str, message: str, color: str) -> None:
    payload = {
        "channel": channel,
        "attachments": [{
            "color": color,
            "text": message,
            "footer": "aws-sla-incident-manager",
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }]
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        log.info("Slack notification sent")
    except Exception as e:
        log.error("Slack notification failed: %s", e)


def _send_email(email_cfg: dict, to_addresses: list[str], subject: str, body: str) -> None:
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = email_cfg["from_address"]
        msg["To"] = ", ".join(to_addresses)
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=15) as smtp:
            smtp.starttls()
            smtp.login(email_cfg["smtp_user"], email_cfg["smtp_password"])
            smtp.sendmail(email_cfg["from_address"], to_addresses, msg.as_string())
        log.info("Email sent to: %s", to_addresses)
    except Exception as e:
        log.error("Email notification failed: %s", e)


def dispatch_notification(policies: dict, incident_id: str, severity: str,
                           title: str, client: str, escalation_level: str) -> None:
    alert_cfg = policies.get("alerts", {})
    client_policy = policies.get("clients", {}).get(client, {})
    contacts = client_policy.get("contacts", {})

    sev_colors = {"SEV1": "#e74c3c", "SEV2": "#f39c12", "SEV3": "#3498db"}
    color = sev_colors.get(severity, "#999999")

    message = (
        f"[{severity}] {incident_id} — {title}\n"
        f"Client: {client.upper()}  |  Escalation: {escalation_level}\n"
        f"Action required: Acknowledge at ops dashboard"
    )
    subject = f"[{severity}] Incident {incident_id}: {title} ({client.upper()})"

    slack_cfg = alert_cfg.get("slack", {})
    if slack_cfg.get("enabled"):
        _send_slack(slack_cfg["webhook_url"], slack_cfg.get("channel", "#ops-incidents"), message, color)

    email_cfg = alert_cfg.get("email", {})
    if email_cfg.get("enabled"):
        recipients = list({contacts.get("primary", ""), contacts.get("escalation", ""),
                           *email_cfg.get("to_addresses", [])})
        recipients = [r for r in recipients if r]
        if recipients:
            _send_email(email_cfg, recipients, subject, message)


def route_incident_alert(incident_id: str, severity: str, title: str, client: str) -> None:
    """Called immediately when a new SEV1/SEV2 incident is created."""
    policies = load_policies()
    dispatch_notification(policies, incident_id, severity, title, client, "L1")
    log.info("Initial alert dispatched for %s [%s] client=%s", incident_id, severity, client)


def check_and_escalate(conn: sqlite3.Connection, policies: dict) -> int:
    """
    Find unacknowledged open incidents past their escalation window and escalate.
    Returns count of incidents escalated.
    """
    escalation_cfg = policies.get("escalation", {})
    l1_to_l2 = escalation_cfg.get("l1_to_l2_minutes", 15)
    l2_to_l3 = escalation_cfg.get("l2_to_l3_minutes", 30)

    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone():
        log.info("No incidents table yet")
        return 0

    open_incidents = conn.execute(
        """SELECT incident_id, severity, title, client, status, created_at, acknowledged_at
           FROM incidents WHERE status IN ('open', 'acknowledged')
           ORDER BY created_at ASC"""
    ).fetchall()

    now = datetime.now(timezone.utc)
    escalated = 0

    for row in open_incidents:
        inc = dict(row)
        created = datetime.fromisoformat(inc["created_at"])
        age_minutes = (now - created).total_seconds() / 60
        client_policy = policies.get("clients", {}).get(inc["client"], {})
        response_sla = client_policy.get("incident_response_minutes", {}).get(
            inc["severity"].lower(), 60
        )

        # Check if past L2 escalation threshold (unacknowledged)
        if inc["status"] == "open" and age_minutes > l1_to_l2:
            log.warning("ESCALATE L2: %s [%s] age=%.0fm unacknowledged", inc["incident_id"], inc["severity"], age_minutes)
            dispatch_notification(policies, inc["incident_id"], inc["severity"], inc["title"], inc["client"], "L2")
            conn.execute(
                "INSERT INTO incident_timeline (incident_id, event_type, note, ts) VALUES (?,?,?,?)",
                (inc["incident_id"], "escalated", f"Escalated to L2 — unacknowledged for {age_minutes:.0f}m",
                 now.isoformat()),
            )
            escalated += 1

        # Check if past L3 threshold (still unacknowledged or acknowledged but unresolved too long)
        elif age_minutes > l1_to_l2 + l2_to_l3 and inc["severity"] in ("SEV1", "SEV2"):
            log.warning("ESCALATE L3: %s [%s] age=%.0fm", inc["incident_id"], inc["severity"], age_minutes)
            dispatch_notification(policies, inc["incident_id"], inc["severity"], inc["title"], inc["client"], "L3")
            conn.execute(
                "INSERT INTO incident_timeline (incident_id, event_type, note, ts) VALUES (?,?,?,?)",
                (inc["incident_id"], "escalated", f"Escalated to L3 — {age_minutes:.0f}m elapsed", now.isoformat()),
            )
            escalated += 1

        # SLA breach warning
        if age_minutes > response_sla and inc["status"] != "resolved":
            log.warning("SLA BREACH RISK: %s response SLA is %dm, incident age is %.0fm",
                        inc["incident_id"], response_sla, age_minutes)

    conn.commit()
    return escalated


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert router and escalation engine")
    parser.add_argument("--check-escalations", action="store_true", help="Check for stale incidents and escalate")
    parser.add_argument("--notify", metavar="INCIDENT_ID", help="Re-send notification for an incident")
    args = parser.parse_args()

    policies = load_policies()
    conn = get_db()

    if args.check_escalations:
        count = check_and_escalate(conn, policies)
        log.info("Escalation check complete. %d escalations triggered.", count)

    elif args.notify:
        row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (args.notify,)).fetchone()
        if not row:
            print(f"Incident {args.notify} not found")
            sys.exit(1)
        r = dict(row)
        dispatch_notification(policies, r["incident_id"], r["severity"], r["title"], r["client"], "manual")
        print(f"Notification dispatched for {args.notify}")


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

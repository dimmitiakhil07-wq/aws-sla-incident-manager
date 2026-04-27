# AWS SLA Incident Manager

An AWS cloud operations toolkit for SLA/SLO compliance tracking, incident lifecycle management, and automated RCA generation. Built from the operational patterns of supporting enterprise clients (retail, hospitality) on a high-availability AWS SaaS platform.

Integrates with AWS CloudWatch to pull EC2, RDS, and ALB metrics, tracks service uptime against defined SLA targets, manages Sev-1/Sev-2 incidents with escalation routing, and auto-generates structured RCA documents at incident close.

---

## What It Does

- **Metric Collection** — Pulls EC2, RDS, and ALB metrics from CloudWatch; stores locally in SQLite for dashboard and reporting
- **SLA/SLO Tracking** — Calculates real uptime percentages against per-client SLA targets; flags breaches; generates weekly/monthly compliance reports
- **Incident Management** — Create, update, and resolve incidents with severity classification, owner assignment, and full timeline tracking
- **Alert Escalation** — Routes alerts through L1 → L2 → L3 escalation paths with time-based thresholds; notifies via email and Slack
- **RCA Generation** — At incident close, auto-generates a structured RCA markdown document populated with timeline, metrics at time of incident, and fix summary
- **Ops Dashboard** — Flask dashboard showing active incidents, SLA compliance gauges, and recent CloudWatch metric trends

---

## Stack

| Layer | Technology |
|---|---|
| AWS integration | Python `boto3` |
| Storage | SQLite |
| Dashboard | Python Flask + Jinja2 |
| Config | YAML |
| Notifications | SMTP / Slack Webhook |
| Scripting | Bash (automation tasks) |

---

## Project Structure

```
aws-sla-incident-manager/
├── config/
│   ├── aws_config.yaml       # AWS regions, resource IDs, client mapping
│   └── sla_policies.yaml     # Per-client SLA targets and escalation rules
├── src/
│   ├── cloudwatch_collector.py   # Pulls metrics from CloudWatch → SQLite
│   ├── sla_tracker.py            # Uptime calculation + SLA report generation
│   ├── incident_manager.py       # Incident CRUD, timeline, MTTR/MTTD
│   ├── alert_router.py           # Escalation engine + notification dispatch
│   ├── rca_generator.py          # Auto-generates RCA markdown from incident data
│   └── dashboard.py              # Flask web dashboard
├── automation/
│   ├── ec2_snapshot.sh           # Pre-maintenance EC2 snapshot script
│   ├── health_check.sh           # Quick AWS resource health check
│   └── log_cleanup.sh            # CloudWatch log group retention setter
├── templates/
│   ├── rca_template.md           # RCA document template (Jinja2)
│   └── dashboard.html            # Dashboard Jinja2 template
├── requirements.txt
├── SETUP.md
└── DOCS.md
```

---

## Quick Start

```bash
pip install -r requirements.txt
cp config/aws_config.yaml config/aws_config.yaml    # edit with your AWS resource IDs

# Collect CloudWatch metrics
python src/cloudwatch_collector.py --once

# Check SLA compliance
python src/sla_tracker.py --report --period monthly

# Start the dashboard
python src/dashboard.py
# Open http://localhost:5000
```

See [SETUP.md](SETUP.md) for AWS credential setup, IAM policy, and full configuration.

---

## Key Workflows

### Daily Ops
1. `cloudwatch_collector.py` runs every 5 minutes (cron) pulling EC2/RDS/ALB metrics
2. Dashboard shows real-time SLA gauges and active incidents
3. Alerts auto-escalate if unacknowledged past configured time windows

### Incident Response
```bash
# Open a new incident
python src/incident_manager.py create --sev SEV1 --title "RDS latency spike" --client 7eleven

# Add a timeline update
python src/incident_manager.py update --id INC-0042 --note "Root cause identified: runaway query"

# Resolve and generate RCA
python src/incident_manager.py resolve --id INC-0042 --fix "Killed query, added index, deployed query timeout config"
# RCA document saved to incidents/INC-0042_RCA.md
```

### SLA Reporting
```bash
python src/sla_tracker.py --report --period monthly --client all
# Outputs: reports/sla_report_2024-03.md
```

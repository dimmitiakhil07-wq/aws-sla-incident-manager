# Architecture & Component Reference — AWS SLA Incident Manager

---

## Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                        AWS Account                                   │
│   EC2 Instances ──┐                                                  │
│   RDS Instances ──┼──▶  CloudWatch Metrics                          │
│   ALB             ──┘         │                                      │
└───────────────────────────────┼──────────────────────────────────────┘
                                │ boto3
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Monitoring Server                                  │
│                                                                      │
│  cloudwatch_collector.py ──▶ SQLite (data/ops.db)                   │
│                                     │                                │
│                    ┌────────────────┼───────────────┐               │
│                    ▼                ▼               ▼               │
│             sla_tracker.py   incident_manager.py  dashboard.py      │
│                    │                │               │               │
│             reports/*.md    incidents/*_RCA.md    :5000             │
│                             │                                        │
│                    alert_router.py                                   │
│                    │           │                                      │
│                  Slack        Email                                   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### `src/cloudwatch_collector.py`

**Purpose:** Pulls metrics from AWS CloudWatch using boto3 and writes them to SQLite.

**Metrics collected:**

| Resource | Metrics |
|---|---|
| EC2 | CPUUtilization, NetworkIn, NetworkOut, StatusCheckFailed |
| RDS | CPUUtilization, DatabaseConnections, FreeStorageSpace, ReadLatency, WriteLatency, FreeableMemory |
| ALB | RequestCount, HTTPCode_ELB_5XX_Count, HTTPCode_ELB_4XX_Count, TargetResponseTime, ActiveConnectionCount |

**Collection strategy:** Uses `GetMetricStatistics` with a lookback window (default 10 min) on each run. Stores one row per resource+metric per collection run. Designed for 5-minute cron intervals matching CloudWatch's default aggregation period.

**Deduplication:** Not implemented — duplicate rows are acceptable given the small volume. The dashboard and SLA tracker both use `AVG()` and `MAX()` queries so duplicates don't skew results.

---

### `src/sla_tracker.py`

**Purpose:** Calculates uptime percentages and response SLOs per client for a given period. Generates Markdown compliance reports.

**Uptime calculation method:**
The tool uses a proxy approach: a 5-minute window is counted as "down" if `StatusCheckFailed > 0` on any EC2 instance for that client, or if the ALB's 5XX count exceeds 50% of total requests in that window.

This is a pragmatic approximation. True uptime tracking would require external synthetic monitoring (CloudWatch Synthetics or a third-party tool). For an internal ops tool, status check failures and 5XX spikes are strong proxies for user-facing outages.

**Report output:** `reports/sla_report_<YYYY-MM>_<period>.md` — plain Markdown, easy to paste into Confluence or email to stakeholders.

---

### `src/incident_manager.py`

**Purpose:** Full incident lifecycle: create, acknowledge, update (timeline notes), and resolve.

**Incident ID format:** `INC-NNNN` (zero-padded sequential integer). Auto-generated, collision-safe within the local SQLite instance.

**MTTR calculation:** `resolved_at - created_at` in minutes, stored as `mttr_minutes` on the incident row.

**MTTD calculation:** `acknowledged_at - created_at` in minutes — represents time-to-detect/acknowledge. Only populated if the incident was acknowledged before being resolved.

**On resolve:** Automatically calls `rca_generator.generate_rca()` to produce the RCA document. Non-blocking — if RCA generation fails, the resolve still completes.

**CLI interface summary:**
```bash
python src/incident_manager.py create --sev SEV1 --title "..." --client 7eleven
python src/incident_manager.py ack    --id INC-0001
python src/incident_manager.py update --id INC-0001 --note "..."
python src/incident_manager.py resolve --id INC-0001 --fix "..."
python src/incident_manager.py list   [--status open] [--client mgm]
python src/incident_manager.py show   --id INC-0001
```

---

### `src/alert_router.py`

**Purpose:** Escalation engine and notification dispatcher.

**Escalation logic:**
- `l1_to_l2_minutes` (default 15): If a SEV1/SEV2 incident is still `open` (unacknowledged) after this many minutes, escalate to L2.
- `l2_to_l3_minutes` (default 30): After an additional 30 minutes without resolution, escalate to L3.
- Escalation events are recorded in `incident_timeline` and notifications re-dispatched.

**Designed to run via cron every 5 minutes:**
```bash
*/5 * * * * python /opt/aws-sla-incident-manager/src/alert_router.py --check-escalations
```

**Notification channels:**
- Slack: single webhook call with colored attachment (red=SEV1, yellow=SEV2, blue=SEV3)
- Email: SMTP with `starttls()`, supports multiple recipients

---

### `src/rca_generator.py`

**Purpose:** Auto-generates a structured Root Cause Analysis document when an incident is resolved.

**What it generates:**
- Incident summary table (ID, severity, client, MTTR, MTTD)
- Full timeline (all events from `incident_timeline`)
- CloudWatch metrics from the incident window ± buffer (min/max/avg per resource+metric)
- Placeholder sections for root cause, contributing factors, action items, and lessons learned

The auto-populated sections (summary, timeline, metrics) are factual and ready to present. The narrative sections require manual completion by the incident commander — this is intentional. RCA narrative should not be auto-generated.

**Output:** `incidents/<INCIDENT_ID>_RCA.md`

---

### `automation/ec2_snapshot.sh`

**Purpose:** Creates EBS snapshots before maintenance windows. Tags snapshots for easy identification and cleanup.

**Tag schema:**
- `Name`: `<instance-name>-<YYYY-MM-DD>`
- `InstanceId`: source instance
- `Reason`: passed via `--reason` flag
- `CreatedBy`: `ec2_snapshot_script`
- `Date`: ISO date

---

### `automation/health_check.sh`

**Purpose:** Quick pre/post-deployment or ad-hoc health check of AWS resources. Returns exit code 1 if any resource is unhealthy — useful as a pipeline gate.

**Checks:**
- EC2 instance status (system + instance checks)
- RDS `DBInstanceStatus == available`
- ALB target group health (all targets healthy)

---

### `automation/log_cleanup.sh`

**Purpose:** Enforces CloudWatch Log Group retention policies and removes empty log groups. Prevents unbounded storage costs in accounts with many Lambda functions or services that create log groups but rarely clean them up.

---

## Database Schema

```sql
cw_metrics (
    id, resource_type, resource_id, resource_name,
    client, metric_name, value, unit, collected_at
)

incidents (
    id, incident_id, severity, title, client, status,
    owner, created_at, acknowledged_at, resolved_at,
    fix_summary, mttr_minutes, mttd_minutes
)

incident_timeline (
    id, incident_id, event_type, note, author, ts
)
```

---

## Configuration Reference

### `config/aws_config.yaml`

| Key | Description |
|---|---|
| `aws.region` | AWS region for all API calls |
| `aws.profile` | AWS CLI profile name |
| `ec2_instances[].id` | EC2 instance ID |
| `ec2_instances[].client` | Client name (must match sla_policies.yaml) |
| `rds_instances[].id` | RDS DB instance identifier |
| `alb_names[].name` | ALB name (not ARN) |
| `collection.lookback_minutes` | How far back to pull metrics per run |
| `collection.period_seconds` | CloudWatch aggregation period |

### `config/sla_policies.yaml`

| Key | Description |
|---|---|
| `clients.<name>.sla_uptime_pct` | Monthly uptime SLA target (e.g. 99.9) |
| `clients.<name>.slo_response_ms` | p95 response time SLO in milliseconds |
| `clients.<name>.incident_response_minutes.sev1` | Response time SLA for SEV1 |
| `clients.<name>.contacts.primary` | Primary contact email |
| `clients.<name>.contacts.escalation` | Escalation contact email |
| `escalation.l1_to_l2_minutes` | Minutes before L2 escalation |
| `escalation.l2_to_l3_minutes` | Additional minutes before L3 escalation |
| `alerts.slack.enabled` | Enable Slack notifications |
| `alerts.email.enabled` | Enable email notifications |

---

## Operational Runbook

### SEV1 Incident Workflow

```
1. Alert fires (CloudWatch alarm, customer report, or health_check.sh)
2. Create incident:
   python src/incident_manager.py create --sev SEV1 --title "..." --client <client>
3. Acknowledge (stops L2 escalation timer):
   python src/incident_manager.py ack --id INC-XXXX
4. Add timeline updates as investigation progresses:
   python src/incident_manager.py update --id INC-XXXX --note "Identified root cause"
5. Resolve with fix summary (triggers RCA generation):
   python src/incident_manager.py resolve --id INC-XXXX --fix "Deployed fix, restarted service"
6. Complete the RCA narrative in incidents/INC-XXXX_RCA.md
7. Share RCA with stakeholders and add to Confluence
```

### Monthly SLA Report Workflow

```
1. Run on the 1st of each month (or via cron):
   python src/sla_tracker.py --report --period monthly
2. Report saved to reports/sla_report_<YYYY-MM>_monthly.md
3. Review for any SLA breaches
4. For breached clients, attach relevant RCA documents
5. Present in monthly ops review
```

# Setup Guide — AWS SLA Incident Manager

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.9+ | |
| AWS account | With CloudWatch, EC2, RDS, ALB access |
| AWS credentials | Configured via `~/.aws/credentials` or IAM role |
| boto3 IAM permissions | See IAM policy below |

---

## Step 1 — Install Dependencies

```bash
git clone https://github.com/your-username/aws-sla-incident-manager.git
cd aws-sla-incident-manager
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Step 2 — AWS Credentials

The tool uses boto3 and reads credentials from the standard AWS credential chain:

**Option A — AWS CLI profile (recommended for local use)**
```bash
aws configure
# or for a named profile:
aws configure --profile prod-monitoring
```

**Option B — IAM Role (recommended for EC2 or Lambda)**
If running on an EC2 instance, attach an IAM role. boto3 automatically picks up instance profile credentials — no config needed.

**Option C — Environment variables**
```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

---

## Step 3 — Minimum IAM Policy

Attach this policy to the IAM user or role used by the tool:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "cloudwatch:GetMetricData",
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:CreateSnapshot",
        "ec2:DescribeSnapshots",
        "rds:DescribeDBInstances",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeTargetHealth",
        "logs:DescribeLogGroups",
        "logs:PutRetentionPolicy"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## Step 4 — Configure AWS Resources

Edit `config/aws_config.yaml`:

```yaml
aws:
  region: us-east-1
  profile: default            # AWS CLI profile name (or 'default')

ec2_instances:
  - id: i-0abc1234def5678
    name: web-server-prod-01
    client: 7eleven
    environment: production

  - id: i-0xyz9876abc1234
    name: app-server-prod-01
    client: bp
    environment: production

rds_instances:
  - id: prod-mysql-01
    name: prod-mysql-01
    client: 7eleven
    environment: production

  - id: prod-pg-mgm-01
    name: prod-pg-mgm-01
    client: mgm
    environment: production

alb_names:
  - name: prod-alb-7eleven
    client: 7eleven
  - name: prod-alb-mgm
    client: mgm
```

---

## Step 5 — Configure SLA Policies

Edit `config/sla_policies.yaml`:

```yaml
clients:
  7eleven:
    sla_uptime_pct: 99.9        # monthly uptime SLA target
    slo_response_ms: 500        # p95 response time target
    incident_response_minutes:
      sev1: 15                  # response SLA for Sev-1
      sev2: 30
      sev3: 120
    contacts:
      primary: ops-7eleven@yourco.com
      escalation: vp-ops@yourco.com

  bp:
    sla_uptime_pct: 99.5
    slo_response_ms: 800
    incident_response_minutes:
      sev1: 15
      sev2: 60
    contacts:
      primary: ops-bp@yourco.com

  mgm:
    sla_uptime_pct: 99.9
    slo_response_ms: 300
    incident_response_minutes:
      sev1: 10
      sev2: 30
    contacts:
      primary: ops-mgm@yourco.com

  spwy:
    sla_uptime_pct: 99.5
    slo_response_ms: 600
    incident_response_minutes:
      sev1: 15
      sev2: 60
    contacts:
      primary: ops-spwy@yourco.com

escalation:
  l1_to_l2_minutes: 15       # escalate to L2 if unacknowledged for 15min
  l2_to_l3_minutes: 30       # escalate to L3 if still unacknowledged for 30min

alerts:
  slack:
    enabled: false
    webhook_url: https://hooks.slack.com/services/CHANGE/ME/NOW
    channel: "#ops-incidents"
  email:
    enabled: false
    smtp_host: smtp.gmail.com
    smtp_port: 587
    smtp_user: alerts@yourco.com
    smtp_password: your-app-password
    from_address: alerts@yourco.com
```

---

## Step 6 — Verify AWS Connectivity

```bash
python -c "
import boto3, yaml
with open('config/aws_config.yaml') as f:
    cfg = yaml.safe_load(f)
session = boto3.Session(profile_name=cfg['aws']['profile'], region_name=cfg['aws']['region'])
cw = session.client('cloudwatch')
print('CloudWatch connected:', cw.list_metrics(Namespace='AWS/EC2')['Metrics'][0]['MetricName'])
"
```

Expected output: `CloudWatch connected: CPUUtilization`

---

## Step 7 — Initial Collection

```bash
# Collect current metrics (creates data/ops.db)
python src/cloudwatch_collector.py --once

# Check what was collected
sqlite3 data/ops.db "SELECT resource_id, metric_name, value, collected_at FROM cw_metrics ORDER BY collected_at DESC LIMIT 10;"
```

---

## Step 8 — Start Dashboard

```bash
python src/dashboard.py
# Open http://localhost:5000
```

---

## Step 9 — Schedule Collection with Cron

```bash
crontab -e
```

```cron
# Collect CloudWatch metrics every 5 minutes
*/5 * * * * /opt/aws-sla-incident-manager/venv/bin/python /opt/aws-sla-incident-manager/src/cloudwatch_collector.py --once >> /var/log/aws-ops/collector.log 2>&1

# Generate SLA report on the 1st of each month
0 8 1 * * /opt/aws-sla-incident-manager/venv/bin/python /opt/aws-sla-incident-manager/src/sla_tracker.py --report --period monthly >> /var/log/aws-ops/sla_report.log 2>&1
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NoCredentialsError` | Run `aws configure` or set `AWS_ACCESS_KEY_ID` env var |
| `AccessDenied` on CloudWatch | Attach the IAM policy from Step 3 |
| No data in dashboard | Run `cloudwatch_collector.py --once` first |
| `ResourceNotFoundException` on RDS | Verify instance IDs in `aws_config.yaml` match actual AWS resource IDs |
| Empty SLA report | Ensure `collected_at` data exists for the target period |

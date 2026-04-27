#!/bin/bash
# health_check.sh
# Quick AWS resource health check — EC2 instance status, RDS availability, ALB target health.
# Outputs a summary to stdout. Exits 1 if any resource is unhealthy.
#
# Usage:
#   ./automation/health_check.sh
#   ./automation/health_check.sh --client 7eleven
#   ./automation/health_check.sh --region us-west-2

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-default}"
CLIENT_FILTER=""
EXIT_CODE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)  REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --client)  CLIENT_FILTER="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

AWS="aws --region $REGION --profile $PROFILE"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
ok()   { echo "  [OK]   $*"; }
fail() { echo "  [FAIL] $*"; EXIT_CODE=1; }
warn() { echo "  [WARN] $*"; }

echo "=============================="
echo "  AWS Resource Health Check"
echo "  Region: $REGION  |  $(date -u)"
echo "=============================="

# ── EC2 Instance Status ───────────────────────────────────────────────────────
echo ""
echo "EC2 Instances:"

FILTER_ARGS="Name=instance-state-name,Values=running"
[[ -n "$CLIENT_FILTER" ]] && FILTER_ARGS="$FILTER_ARGS Name=tag:client,Values=$CLIENT_FILTER"

instance_data=$($AWS ec2 describe-instance-status \
    --filters "$FILTER_ARGS" \
    --include-all-instances \
    --query 'InstanceStatuses[].{id:InstanceId,system:SystemStatus.Status,instance:InstanceStatus.Status}' \
    --output json 2>/dev/null || echo "[]")

if [[ "$instance_data" == "[]" ]]; then
    warn "No running instances found${CLIENT_FILTER:+ for client=$CLIENT_FILTER}"
else
    echo "$instance_data" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for item in data:
    iid = item['id']
    sys_status = item.get('system', 'unknown')
    inst_status = item.get('instance', 'unknown')
    if sys_status == 'ok' and inst_status == 'ok':
        print(f'  [OK]   {iid}  system:{sys_status}  instance:{inst_status}')
    else:
        print(f'  [FAIL] {iid}  system:{sys_status}  instance:{inst_status}')
        sys.exit_code_flag = True
"
fi

# ── RDS Instance Status ───────────────────────────────────────────────────────
echo ""
echo "RDS Instances:"

rds_data=$($AWS rds describe-db-instances \
    --query 'DBInstances[].{id:DBInstanceIdentifier,status:DBInstanceStatus,engine:Engine}' \
    --output json 2>/dev/null || echo "[]")

if [[ "$rds_data" == "[]" ]]; then
    warn "No RDS instances found"
else
    while IFS= read -r line; do
        db_id=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['id'])")
        db_status=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['status'])")
        db_engine=$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['engine'])")
        if [[ "$db_status" == "available" ]]; then
            ok "$db_id  ($db_engine)  status:$db_status"
        else
            fail "$db_id  ($db_engine)  status:$db_status"
        fi
    done < <(echo "$rds_data" | python3 -c "
import sys, json
for item in json.load(sys.stdin):
    print(json.dumps(item))
")
fi

# ── ALB Target Group Health ───────────────────────────────────────────────────
echo ""
echo "ALB Target Groups:"

tg_arns=$($AWS elbv2 describe-target-groups \
    --query 'TargetGroups[].TargetGroupArn' \
    --output text 2>/dev/null || echo "")

if [[ -z "$tg_arns" ]]; then
    warn "No target groups found"
else
    for tg_arn in $tg_arns; do
        tg_name=$(echo "$tg_arn" | grep -oP '(?<=targetgroup/)[^/]+' || echo "$tg_arn")
        health=$($AWS elbv2 describe-target-health \
            --target-group-arn "$tg_arn" \
            --query 'TargetHealthDescriptions[].{id:Target.Id,state:TargetHealth.State}' \
            --output json 2>/dev/null || echo "[]")

        unhealthy=$(echo "$health" | python3 -c "
import sys,json
data = json.load(sys.stdin)
bad = [d for d in data if d.get('state') != 'healthy']
print(len(bad))
" 2>/dev/null || echo "0")

        total=$(echo "$health" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

        if [[ "$unhealthy" == "0" && "$total" -gt 0 ]]; then
            ok "$tg_name  $total/$total targets healthy"
        elif [[ "$total" == "0" ]]; then
            warn "$tg_name  no registered targets"
        else
            fail "$tg_name  $((total - unhealthy))/$total targets healthy — $unhealthy unhealthy"
        fi
    done
fi

echo ""
echo "=============================="
if [[ $EXIT_CODE -eq 0 ]]; then
    echo "  RESULT: All checks passed"
else
    echo "  RESULT: UNHEALTHY resources detected"
fi
echo "=============================="
exit $EXIT_CODE

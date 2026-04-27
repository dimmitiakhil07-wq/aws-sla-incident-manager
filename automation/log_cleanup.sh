#!/bin/bash
# log_cleanup.sh
# Sets CloudWatch Log Group retention policies and optionally deletes empty log groups.
# Prevents unbounded log storage costs by enforcing retention across all log groups.
#
# Usage:
#   ./automation/log_cleanup.sh --retention 30                  # set 30-day retention everywhere
#   ./automation/log_cleanup.sh --retention 90 --prefix /aws/lambda
#   ./automation/log_cleanup.sh --delete-empty --dry-run

set -euo pipefail

RETENTION_DAYS=30
PREFIX=""
DELETE_EMPTY=false
DRY_RUN=false
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-default}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --retention)    RETENTION_DAYS="$2"; shift 2 ;;
        --prefix)       PREFIX="$2"; shift 2 ;;
        --delete-empty) DELETE_EMPTY=true; shift ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --region)       REGION="$2"; shift 2 ;;
        --profile)      PROFILE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

AWS="aws --region $REGION --profile $PROFILE"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
dry() { $DRY_RUN && echo "[DRY-RUN] $*" || true; }

log "CloudWatch log group retention cleanup"
log "Retention: ${RETENTION_DAYS} days | Prefix: '${PREFIX:-all}' | Dry-run: $DRY_RUN"

updated=0
skipped=0
deleted=0

while IFS= read -r group_name; do
    [[ -z "$group_name" ]] && continue

    # Get current retention
    current=$($AWS logs describe-log-groups \
        --log-group-name-prefix "$group_name" \
        --query 'logGroups[0].retentionInDays' \
        --output text 2>/dev/null || echo "None")

    if [[ "$current" == "$RETENTION_DAYS" ]]; then
        skipped=$((skipped + 1))
        continue
    fi

    if $DRY_RUN; then
        log "[DRY-RUN] Would set retention=$RETENTION_DAYS on: $group_name (current: $current)"
    else
        $AWS logs put-retention-policy \
            --log-group-name "$group_name" \
            --retention-in-days "$RETENTION_DAYS" 2>/dev/null || true
        log "Set retention=$RETENTION_DAYS: $group_name (was: $current)"
    fi
    updated=$((updated + 1))

done < <($AWS logs describe-log-groups \
    ${PREFIX:+--log-group-name-prefix "$PREFIX"} \
    --query 'logGroups[].logGroupName' \
    --output text 2>/dev/null | tr '\t' '\n')

# Delete empty log groups
if $DELETE_EMPTY; then
    log "Scanning for empty log groups..."
    while IFS= read -r group_name; do
        [[ -z "$group_name" ]] && continue
        stored=$($AWS logs describe-log-groups \
            --log-group-name-prefix "$group_name" \
            --query 'logGroups[0].storedBytes' \
            --output text 2>/dev/null || echo "1")

        if [[ "$stored" == "0" || "$stored" == "None" ]]; then
            if $DRY_RUN; then
                log "[DRY-RUN] Would delete empty: $group_name"
            else
                $AWS logs delete-log-group --log-group-name "$group_name" 2>/dev/null || true
                log "Deleted empty log group: $group_name"
            fi
            deleted=$((deleted + 1))
        fi
    done < <($AWS logs describe-log-groups \
        ${PREFIX:+--log-group-name-prefix "$PREFIX"} \
        --query 'logGroups[].logGroupName' \
        --output text 2>/dev/null | tr '\t' '\n')
fi

log "Done — updated: $updated  skipped: $skipped  deleted: $deleted"

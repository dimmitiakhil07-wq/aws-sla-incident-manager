#!/bin/bash
# ec2_snapshot.sh
# Creates EBS snapshots for specified EC2 instances before maintenance windows.
# Tags snapshots with instance name, date, and reason for easy filtering/cleanup.
#
# Usage:
#   ./automation/ec2_snapshot.sh --instance-id i-0abc1234 --reason "pre-maintenance"
#   ./automation/ec2_snapshot.sh --all-production --reason "weekly-backup"
#
# Requires: AWS CLI configured with ec2:CreateSnapshot, ec2:DescribeInstances permissions

set -euo pipefail

INSTANCE_ID=""
ALL_PRODUCTION=false
REASON="manual"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PROFILE="${AWS_PROFILE:-default}"

usage() {
    echo "Usage: $0 [--instance-id ID | --all-production] [--reason TEXT] [--region REGION]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --instance-id)     INSTANCE_ID="$2"; shift 2 ;;
        --all-production)  ALL_PRODUCTION=true; shift ;;
        --reason)          REASON="$2"; shift 2 ;;
        --region)          REGION="$2"; shift 2 ;;
        --profile)         PROFILE="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[[ -z "$INSTANCE_ID" && "$ALL_PRODUCTION" = false ]] && usage

AWS="aws --region $REGION --profile $PROFILE"
DATE=$(date -u +"%Y-%m-%d")
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

snapshot_instance() {
    local instance_id="$1"

    # Get instance name tag
    instance_name=$($AWS ec2 describe-instances \
        --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].Tags[?Key==`Name`].Value' \
        --output text 2>/dev/null || echo "$instance_id")

    # Get all attached EBS volume IDs
    volume_ids=$($AWS ec2 describe-instances \
        --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].BlockDeviceMappings[].Ebs.VolumeId' \
        --output text 2>/dev/null)

    if [[ -z "$volume_ids" ]]; then
        log "WARNING: No EBS volumes found for $instance_id — skipping"
        return
    fi

    for volume_id in $volume_ids; do
        log "Creating snapshot: $volume_id ($instance_name) ..."
        snapshot_id=$($AWS ec2 create-snapshot \
            --volume-id "$volume_id" \
            --description "Automated snapshot: $instance_name | $REASON | $DATE" \
            --tag-specifications "ResourceType=snapshot,Tags=[
                {Key=Name,Value=${instance_name}-${DATE}},
                {Key=InstanceId,Value=${instance_id}},
                {Key=Reason,Value=${REASON}},
                {Key=CreatedBy,Value=ec2_snapshot_script},
                {Key=Date,Value=${DATE}}
            ]" \
            --query 'SnapshotId' \
            --output text)

        log "Created snapshot: $snapshot_id for volume $volume_id ($instance_name)"
    done
}

if [[ -n "$INSTANCE_ID" ]]; then
    snapshot_instance "$INSTANCE_ID"
elif [[ "$ALL_PRODUCTION" = true ]]; then
    log "Fetching all production EC2 instances..."
    instance_ids=$($AWS ec2 describe-instances \
        --filters "Name=tag:environment,Values=production" "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].InstanceId' \
        --output text)

    if [[ -z "$instance_ids" ]]; then
        log "No running production instances found (tag environment=production)"
        exit 0
    fi

    for iid in $instance_ids; do
        snapshot_instance "$iid"
    done
fi

log "Snapshot job complete."

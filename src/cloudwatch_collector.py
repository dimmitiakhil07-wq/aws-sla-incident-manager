"""
cloudwatch_collector.py — Pulls metrics from AWS CloudWatch and stores them locally.

Collects:
  - EC2: CPUUtilization, NetworkIn, NetworkOut, StatusCheckFailed
  - RDS: CPUUtilization, DatabaseConnections, FreeStorageSpace, ReadLatency, WriteLatency
  - ALB: RequestCount, HTTPCode_ELB_5XX_Count, TargetResponseTime

Usage:
    python src/cloudwatch_collector.py --once
    python src/cloudwatch_collector.py --interval 300
    python src/cloudwatch_collector.py --resource-type ec2 --once
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
import yaml

BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "ops.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_DIR / "aws_config.yaml") as f:
        return yaml.safe_load(f)


def get_boto_session(cfg: dict):
    aws = cfg.get("aws", {})
    return boto3.Session(
        profile_name=aws.get("profile", "default"),
        region_name=aws.get("region", "us-east-1"),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cw_metrics (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_type TEXT NOT NULL,
            resource_id   TEXT NOT NULL,
            resource_name TEXT,
            client        TEXT,
            metric_name   TEXT NOT NULL,
            value         REAL,
            unit          TEXT,
            collected_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_status (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_type TEXT,
            resource_id   TEXT,
            resource_name TEXT,
            client        TEXT,
            status        TEXT,
            checked_at    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cw_resource ON cw_metrics(resource_id, metric_name);
        CREATE INDEX IF NOT EXISTS idx_cw_collected ON cw_metrics(collected_at);
        CREATE INDEX IF NOT EXISTS idx_cw_client ON cw_metrics(client);
    """)
    conn.commit()


def get_metric_avg(cw_client, namespace: str, metric_name: str,
                   dimensions: list, period_sec: int, lookback_minutes: int) -> Optional[float]:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    try:
        resp = cw_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=period_sec,
            Statistics=["Average"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            return None
        return sum(d["Average"] for d in datapoints) / len(datapoints)
    except Exception as e:
        log.debug("CloudWatch error for %s/%s: %s", namespace, metric_name, e)
        return None


def get_metric_sum(cw_client, namespace: str, metric_name: str,
                   dimensions: list, period_sec: int, lookback_minutes: int) -> Optional[float]:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    try:
        resp = cw_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=period_sec,
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            return None
        return sum(d["Sum"] for d in datapoints)
    except Exception as e:
        log.debug("CloudWatch error for %s/%s: %s", namespace, metric_name, e)
        return None


def collect_ec2_metrics(cw_client, conn: sqlite3.Connection, instances: list,
                        period_sec: int, lookback_minutes: int) -> int:
    stored = 0
    now = datetime.now(timezone.utc).isoformat()

    for inst in instances:
        instance_id = inst["id"]
        name = inst.get("name", instance_id)
        client = inst.get("client", "unknown")
        dims = [{"Name": "InstanceId", "Value": instance_id}]

        metrics = [
            ("CPUUtilization",    "AWS/EC2", "avg", "Percent"),
            ("NetworkIn",         "AWS/EC2", "avg", "Bytes"),
            ("NetworkOut",        "AWS/EC2", "avg", "Bytes"),
            ("StatusCheckFailed", "AWS/EC2", "sum", "Count"),
        ]

        for metric_name, namespace, stat_type, unit in metrics:
            if stat_type == "avg":
                value = get_metric_avg(cw_client, namespace, metric_name, dims, period_sec, lookback_minutes)
            else:
                value = get_metric_sum(cw_client, namespace, metric_name, dims, period_sec, lookback_minutes)

            if value is not None:
                conn.execute(
                    """INSERT INTO cw_metrics
                       (resource_type, resource_id, resource_name, client, metric_name, value, unit, collected_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("ec2", instance_id, name, client, metric_name, value, unit, now),
                )
                stored += 1
                log.debug("EC2 %s %s: %.2f %s", name, metric_name, value, unit)

    conn.commit()
    return stored


def collect_rds_metrics(cw_client, conn: sqlite3.Connection, instances: list,
                        period_sec: int, lookback_minutes: int) -> int:
    stored = 0
    now = datetime.now(timezone.utc).isoformat()

    for inst in instances:
        db_id = inst["id"]
        name = inst.get("name", db_id)
        client = inst.get("client", "unknown")
        dims = [{"Name": "DBInstanceIdentifier", "Value": db_id}]

        metrics = [
            ("CPUUtilization",     "avg", "Percent"),
            ("DatabaseConnections","avg", "Count"),
            ("FreeStorageSpace",   "avg", "Bytes"),
            ("ReadLatency",        "avg", "Seconds"),
            ("WriteLatency",       "avg", "Seconds"),
            ("FreeableMemory",     "avg", "Bytes"),
        ]

        for metric_name, stat_type, unit in metrics:
            value = get_metric_avg(cw_client, "AWS/RDS", metric_name, dims, period_sec, lookback_minutes)
            if value is not None:
                conn.execute(
                    """INSERT INTO cw_metrics
                       (resource_type, resource_id, resource_name, client, metric_name, value, unit, collected_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("rds", db_id, name, client, metric_name, value, unit, now),
                )
                stored += 1

    conn.commit()
    return stored


def collect_alb_metrics(cw_client, conn: sqlite3.Connection, alb_list: list,
                        period_sec: int, lookback_minutes: int) -> int:
    stored = 0
    now = datetime.now(timezone.utc).isoformat()

    for alb in alb_list:
        alb_name = alb["name"]
        client = alb.get("client", "unknown")
        dims = [{"Name": "LoadBalancer", "Value": f"app/{alb_name}/placeholder"}]

        metrics = [
            ("RequestCount",           "sum", "Count"),
            ("HTTPCode_ELB_5XX_Count", "sum", "Count"),
            ("HTTPCode_ELB_4XX_Count", "sum", "Count"),
            ("TargetResponseTime",     "avg", "Seconds"),
            ("ActiveConnectionCount",  "avg", "Count"),
        ]

        for metric_name, stat_type, unit in metrics:
            if stat_type == "sum":
                value = get_metric_sum(cw_client, "AWS/ApplicationELB", metric_name, dims, period_sec, lookback_minutes)
            else:
                value = get_metric_avg(cw_client, "AWS/ApplicationELB", metric_name, dims, period_sec, lookback_minutes)

            if value is not None:
                conn.execute(
                    """INSERT INTO cw_metrics
                       (resource_type, resource_id, resource_name, client, metric_name, value, unit, collected_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("alb", alb_name, alb_name, client, metric_name, value, unit, now),
                )
                stored += 1

    conn.commit()
    return stored


def run_collection(cfg: dict, conn: sqlite3.Connection, resource_type: Optional[str] = None) -> int:
    session = get_boto_session(cfg)
    cw = session.client("cloudwatch")
    collection_cfg = cfg.get("collection", {})
    period_sec = collection_cfg.get("period_seconds", 300)
    lookback_min = collection_cfg.get("lookback_minutes", 10)

    total = 0
    if resource_type in (None, "ec2"):
        count = collect_ec2_metrics(cw, conn, cfg.get("ec2_instances", []), period_sec, lookback_min)
        log.info("EC2: %d metric points stored", count)
        total += count

    if resource_type in (None, "rds"):
        count = collect_rds_metrics(cw, conn, cfg.get("rds_instances", []), period_sec, lookback_min)
        log.info("RDS: %d metric points stored", count)
        total += count

    if resource_type in (None, "alb"):
        count = collect_alb_metrics(cw, conn, cfg.get("alb_names", []), period_sec, lookback_min)
        log.info("ALB: %d metric points stored", count)
        total += count

    log.info("Collection complete. Total: %d metric points", total)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="CloudWatch metric collector")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true")
    group.add_argument("--interval", type=int, metavar="SECONDS")
    parser.add_argument("--resource-type", choices=["ec2", "rds", "alb"], help="Collect only this resource type")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config()
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    if args.once:
        run_collection(cfg, conn, args.resource_type)
        return

    log.info("Starting continuous collection every %ds", args.interval)
    while True:
        try:
            run_collection(cfg, conn, args.resource_type)
        except Exception as e:
            log.exception("Collection error: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.path.insert(0, str(BASE_DIR))
    main()

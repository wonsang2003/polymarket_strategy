"""AWS Lambda entry points for Polymarket automated systems.

Two handlers, triggered by separate EventBridge schedules:

1. whale_monitor_handler  — hourly, polls whale trades, sends Telegram alerts
2. weather_autotrade_handler — every 6h, runs full analyze→execute→settle cycle

Environment variables (set in Lambda console):
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — your personal/group chat ID
    S3_STATE_BUCKET     — S3 bucket for state persistence (required for autotrade)

    # Whale monitor specific:
    WHALE_MIN_SIZE      — minimum trade notional to alert on (default: 1000)

    # Weather autotrade specific:
    TRADE_MODE          — "paper" or "live" (default: paper)
    CONFIRM_LIVE        — set to "true" to allow live trading
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Whale Monitor (existing)
# ---------------------------------------------------------------------------

def whale_monitor_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state_path = Path(tempfile.gettempdir()) / "whale_monitor_state.json"

    s3_bucket = os.getenv("S3_STATE_BUCKET", "")
    s3_key = os.getenv("S3_STATE_KEY", "whale_monitor_state.json")
    if s3_bucket:
        _s3_download(s3_bucket, s3_key, state_path)

    min_size = float(os.getenv("WHALE_MIN_SIZE", "1000"))

    from polymarket_strat.monitor import run_monitor

    result = run_monitor(min_size=min_size, state_path=str(state_path))

    if s3_bucket and state_path.exists():
        _s3_upload(s3_bucket, s3_key, state_path)

    return {"statusCode": 200, "body": json.dumps(result, default=str)}


# Backward-compatible alias
handler = whale_monitor_handler


# ---------------------------------------------------------------------------
# Weather Autotrade
# ---------------------------------------------------------------------------

def weather_autotrade_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Full weather autotrade cycle on Lambda.

    Pulls SQLite DB and portfolio state from S3, runs the cycle,
    then pushes updated state back.  EventBridge: rate(6 hours).
    """
    s3_bucket = os.getenv("S3_STATE_BUCKET", "")
    if not s3_bucket:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "S3_STATE_BUCKET env var required for autotrade Lambda"}),
        }

    tmp = Path(tempfile.gettempdir())
    db_path = tmp / "weather.db"
    state_path = tmp / "portfolio_state.json"

    # Pull persistent state from S3
    _s3_download(s3_bucket, "weather/weather.db", db_path)
    _s3_download(s3_bucket, "weather/portfolio_state.json", state_path)

    # Point the weather DB to /tmp
    os.environ["WEATHER_DB_PATH"] = str(db_path)

    mode = os.getenv("TRADE_MODE", "paper")
    confirm_live = os.getenv("CONFIRM_LIVE", "") == "true"

    from polymarket_strat.main import run_autotrade

    result = run_autotrade(
        mode=mode,
        confirm_live=confirm_live,
        state_path=str(state_path),
        env_file="",  # env vars already set in Lambda
    )

    # Push state back to S3
    if db_path.exists():
        _s3_upload(s3_bucket, "weather/weather.db", db_path)
    if state_path.exists():
        _s3_upload(s3_bucket, "weather/portfolio_state.json", state_path)

    return {"statusCode": 200, "body": json.dumps(result, default=str)}


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_download(bucket: str, key: str, local_path: Path) -> None:
    try:
        import boto3

        s3 = boto3.client("s3")
        s3.download_file(bucket, key, str(local_path))
    except Exception:
        pass  # First run or missing state — start fresh


def _s3_upload(bucket: str, key: str, local_path: Path) -> None:
    import boto3

    s3 = boto3.client("s3")
    s3.upload_file(str(local_path), bucket, key)

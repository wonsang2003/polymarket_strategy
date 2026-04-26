"""Diagnose dashboard freshness. Run on EC2."""
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone


def run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"FAIL: {e!r}"


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"now_utc = {now.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 1. Service state
    print("[1] systemctl status polymarket-dashboard")
    print(run(["systemctl", "status", "polymarket-dashboard.service",
               "--no-pager", "--lines=8"]))
    print()

    # 2. Process info
    print("[2] dashboard process")
    print(run(["bash", "-c", "ps -ef | grep streamlit | grep -v grep"]))
    print()

    # 3. Port 8501 listening
    print("[3] port 8501 listener")
    print(run(["bash", "-c", "ss -tnlp 2>/dev/null | grep 8501"]))
    print()

    # 4. Last journalctl errors
    print("[4] last 15 journal lines")
    print(run(["bash", "-c",
               "sudo journalctl -u polymarket-dashboard.service "
               "-n 15 --no-pager 2>&1"]))
    print()

    # 5. Test HTTP endpoint
    print("[5] HTTP health on localhost:8501")
    print(run(["curl", "-sS", "-o", "/dev/null", "-w",
               "code=%{http_code} time=%{time_total}s\n",
               "http://127.0.0.1:8501"]))
    print()

    # 6. DB freshness vs latest trade
    print("[6] DB freshness")
    c = sqlite3.connect("data/weather/weather.db")
    latest_create = c.execute("SELECT MAX(created_at) FROM trade_history").fetchone()[0]
    latest_settle = c.execute("SELECT MAX(settled_at) FROM trade_history WHERE settled_at IS NOT NULL").fetchone()[0]
    open_count = c.execute("SELECT COUNT(*) FROM trade_history WHERE outcome IS NULL").fetchone()[0]
    settled_today = c.execute(
        "SELECT COUNT(*) FROM trade_history WHERE date(settled_at) = date('now')"
    ).fetchone()[0]
    print(f"  latest created_at  : {latest_create}")
    print(f"  latest settled_at  : {latest_settle}")
    print(f"  open positions     : {open_count}")
    print(f"  settled today      : {settled_today}")
    print()

    # 7. Streamlit cache settings — peek at the dashboard module
    print("[7] dashboard cache TTL config")
    print(run(["bash", "-c",
               "grep -n 'ttl\\|cache_data\\|autorefresh' "
               "tools/dashboard/app.py | head -20"]))
    print()

    # 8. Check if streamlit-autorefresh is installed
    print("[8] streamlit-autorefresh installed?")
    print(run(["bash", "-c",
               "venv/bin/pip show streamlit-autorefresh 2>&1 | head -5"]))
    print()

    # 9. Test the SQLite open in same way dashboard does
    print("[9] dashboard-style read test (mode=ro)")
    try:
        ro = sqlite3.connect("file:data/weather/weather.db?mode=ro", uri=True)
        n = ro.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
        ro.close()
        print(f"  OK: trade_history rows = {n}")
    except Exception as e:
        print(f"  FAIL: {e!r}")
    print()

    # 10. Check service restart count (excessive = something's still crashing)
    print("[10] restart count")
    print(run(["bash", "-c",
               "systemctl show polymarket-dashboard.service "
               "-p NRestarts -p ActiveEnterTimestamp"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())

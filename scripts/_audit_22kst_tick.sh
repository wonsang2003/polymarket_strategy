#!/usr/bin/env bash
cd /home/ubuntu/polymarket

echo "=== 1) Did the autotrade cron actually run at 22:00 KST? ==="
ls -la logs/autotrade.log 2>&1 | head -2
tail -200 logs/autotrade.log | grep -E "^\[autotrade\]|^\[" | tail -30

echo
echo "=== 2) What was in the most recent cycle JSON output? ==="
# The JSON cycle is printed at end of each tick. Look for last few cycles.
grep -oE '\{"mode":.*\}' logs/autotrade.log | tail -3 | head -c 4000
echo

echo
echo "=== 3) Recent tick timing ==="
grep "^\[autotrade\] Step" logs/autotrade.log | tail -25

echo
echo "=== 4) Tail strategy diagnostic ==="
grep -E "tail strategy|tail_strategy|tail_ece|absurd_edge|plan_b_high_p" logs/autotrade.log | tail -20

echo
echo "=== 5) Any errors/warnings? ==="
grep -iE "error|exception|traceback|fail" logs/autotrade.log | tail -20

echo
echo "=== 6) What did this cycle scan + reject? ==="
grep -E "gate_rejects|signals|executable|new_trade_count" logs/autotrade.log | tail -20

echo
echo "=== 7) Current local time per city (for tail eligibility check) ==="
date -u +"UTC: %F %T  (now)"
date +"KST: %F %T"
venv/bin/python <<'PY'
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from polymarket_strat.domain.weather.models import CITY_REGISTRY

now_utc = datetime.now(timezone.utc)
print(f"\n{'city':<14} {'tz':<22} {'local time':<10} {'past 4pm':<8}")
for city_key in sorted(CITY_REGISTRY.keys()):
    s = CITY_REGISTRY[city_key]
    try:
        tz = ZoneInfo(s.timezone)
        local = now_utc.astimezone(tz)
        past_peak = local.hour >= 16
        # Approx settlement: 17:00 local
        settle_h = 17
        if local.hour >= settle_h:
            tag = "POST-settle"
        elif past_peak:
            tag = "POST-peak (TAIL ELIGIBLE)"
        else:
            tag = "pre-peak"
        print(f"{city_key:<14} {s.timezone:<22} {local.strftime('%H:%M'):<10} {tag}")
    except Exception as e:
        print(f"{city_key}: tz error: {e}")
PY

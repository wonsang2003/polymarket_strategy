#!/usr/bin/env bash
# Reopen the 6 Apr 22 rows settled prematurely on 2026-04-22 16:23 UTC
# (= 2026-04-23 01:23 KST). Toronto/NYC/Amsterdam/Munich/SaoPaulo/BuenosAires
# were all still in their own local Apr 22 day at that instant — IEM fallback
# fired on partial-day data and froze them. Wellington had actually ended
# (04:23 NZST, past grace), so it stays settled.
#
# Safe to re-run: the UPDATE only targets rows still bearing the exact
# settled_at=2026-04-22 16:23:xx signature that identifies the bug-run.
set -eu
REPO=/home/ubuntu/polymarket
DB=$REPO/data/weather/weather.db

echo "===== Snapshot pre-reopen DB ====="
cp "$DB" "$REPO/data/weather/weather.pre_reopen_$(date +%Y%m%d_%H%M%S).db"

echo
echo "===== Candidate rows (premature settlements at Apr 22 16:23 UTC) ====="
sqlite3 -header -column "$DB" "
  SELECT id, city, target_date, side, entry_price, notional, outcome, ROUND(pnl,2) pnl, substr(settled_at,1,19) settled_at
    FROM trade_history
   WHERE settled_at LIKE '2026-04-22 16:2%'
     AND city IN ('toronto','nyc','amsterdam','munich','sao_paulo','buenos_aires')
     AND target_date = '2026-04-22'
   ORDER BY id;
"

echo
echo "===== Reopen ====="
sqlite3 "$DB" "
  UPDATE trade_history
     SET outcome = NULL, pnl = NULL, settled_at = NULL
   WHERE settled_at LIKE '2026-04-22 16:2%'
     AND city IN ('toronto','nyc','amsterdam','munich','sao_paulo','buenos_aires')
     AND target_date = '2026-04-22';
"
CHANGED=$(sqlite3 "$DB" "SELECT changes();")
echo "  rows reopened: (sqlite doesn't expose changes() across sessions — verify below)"

echo
echo "===== Post-reopen state ====="
sqlite3 "$DB" "
  SELECT COUNT(*) || ' total / ' || SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) || ' open / ' || SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) || ' settled, cum_pnl=' || ROUND(SUM(COALESCE(pnl,0)),2) FROM trade_history;
"
echo
echo "===== Open positions by target_date ====="
sqlite3 -header -column "$DB" "
  SELECT target_date, COUNT(*) n FROM trade_history WHERE outcome IS NULL GROUP BY target_date ORDER BY target_date;
"

exit 0

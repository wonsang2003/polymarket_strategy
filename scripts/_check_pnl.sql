.headers on
.mode column
SELECT
  COUNT(*) n_total,
  SUM(CASE WHEN outcome IS NULL THEN 1 ELSE 0 END) n_open,
  SUM(CASE WHEN outcome=1 THEN 1 ELSE 0 END) n_wins,
  SUM(CASE WHEN outcome=0 THEN 1 ELSE 0 END) n_losses,
  SUM(CASE WHEN outcome=2 THEN 1 ELSE 0 END) n_rebal,
  ROUND(SUM(CASE WHEN outcome IN (0,1) THEN pnl ELSE 0 END),2) settled_pnl,
  ROUND(SUM(CASE WHEN outcome=2 THEN pnl ELSE 0 END),2) rebal_pnl,
  ROUND(SUM(pnl),2) cum_pnl
FROM trade_history;
SELECT '---' AS sep;
SELECT DATE(settled_at) day,
       COUNT(*) n,
       SUM(CASE WHEN outcome=1 THEN 1 ELSE 0 END) wins,
       SUM(CASE WHEN outcome=0 THEN 1 ELSE 0 END) losses,
       SUM(CASE WHEN outcome=2 THEN 1 ELSE 0 END) rebal,
       ROUND(SUM(pnl),2) day_pnl
FROM trade_history
WHERE pnl IS NOT NULL
GROUP BY day
ORDER BY day DESC
LIMIT 10;

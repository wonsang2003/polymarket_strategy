.headers on
.mode column
SELECT
  id,
  city,
  substr(created_at,1,10) entered,
  substr(settled_at,1,10) closed,
  ROUND(model_prob,2) p,
  ROUND(market_prob,2) m,
  ROUND(edge,2) edge_pct,
  exit_reason,
  ROUND(pnl,2) pnl
FROM trade_history
WHERE substr(settled_at,1,10) = '2026-04-25'
ORDER BY id DESC
LIMIT 20;

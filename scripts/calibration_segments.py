"""Audit existing calibration signal per segment.

Question: at what granularity (city, lead, edge bucket, model_prob bucket)
do we have enough trade history to fit a reliable bias-correction layer?

Sample-size analysis precedes design choice."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path("/home/ubuntu/polymarket/data/weather/weather.db")


def conn():
    c = sqlite3.connect(f"file:{DB}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def hr(t):
    print()
    print("=" * 78)
    print(f"  {t}")
    print("=" * 78)


def main():
    c = conn()

    # Population: settled (non-rebal), non-legacy trades.
    # Legacy `<null>` category was a different strategy era — exclude.
    base_filter = """
        outcome IS NOT NULL AND outcome != 2
        AND category IN ('weather', 'weather_tail_no')
        AND model_prob IS NOT NULL AND edge IS NOT NULL
    """

    total = c.execute(f"SELECT COUNT(*) AS n FROM trade_history WHERE {base_filter}").fetchone()
    print(f"  Population (post-fix non-rebal): {total['n']} trades\n")

    # ---- 1. Per-city
    hr("1. PER-CITY (sample-size sanity)")
    rows = c.execute(f"""
        SELECT city, COUNT(*) AS n,
               ROUND(AVG(model_prob), 3) AS avg_pred,
               ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
               ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE {base_filter}
        GROUP BY city
        ORDER BY net
    """).fetchall()
    print(f"  {'city':<14}{'n':>4}  {'avg_pred':>9}  {'realized_wr':>12}  {'gap':>8}  {'net':>10}")
    for r in rows:
        gap = (r['realized_wr'] or 0) - (r['avg_pred'] or 0)
        print(f"  {r['city']:<14}{r['n']:>4}  {r['avg_pred']:>9.3f}  "
              f"{r['realized_wr']:>11.3f}  {gap:>+7.3f}  ${r['net']:>+8.2f}")

    # ---- 2. Per-lead (target_date − created_date)
    hr("2. PER-LEAD-DAYS")
    rows = c.execute(f"""
        SELECT
          CAST(julianday(target_date) - julianday(date(created_at)) AS INT) AS lead_d,
          COUNT(*) AS n,
          ROUND(AVG(model_prob), 3) AS avg_pred,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE {base_filter}
        GROUP BY lead_d
        ORDER BY lead_d
    """).fetchall()
    print(f"  {'lead_d':>6}  {'n':>4}  {'avg_pred':>9}  {'realized_wr':>12}  {'gap':>8}  {'net':>10}")
    for r in rows:
        gap = (r['realized_wr'] or 0) - (r['avg_pred'] or 0)
        print(f"  {r['lead_d']:>6}  {r['n']:>4}  {r['avg_pred']:>9.3f}  "
              f"{r['realized_wr']:>11.3f}  {gap:>+7.3f}  ${r['net']:>+8.2f}")

    # ---- 3. Per model_prob bucket (Platt/isotonic input)
    hr("3. PER MODEL_PROB BUCKET (calibration curve)")
    rows = c.execute(f"""
        SELECT
          CASE
            WHEN model_prob < 0.55 THEN '0.50-0.55'
            WHEN model_prob < 0.60 THEN '0.55-0.60'
            WHEN model_prob < 0.65 THEN '0.60-0.65'
            WHEN model_prob < 0.70 THEN '0.65-0.70'
            WHEN model_prob < 0.75 THEN '0.70-0.75'
            WHEN model_prob < 0.80 THEN '0.75-0.80'
            WHEN model_prob < 0.85 THEN '0.80-0.85'
            WHEN model_prob < 0.90 THEN '0.85-0.90'
            ELSE '0.90+'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(model_prob), 3) AS avg_pred,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE {base_filter}
        GROUP BY bucket
        ORDER BY bucket
    """).fetchall()
    print(f"  {'bucket':<11}{'n':>4}  {'avg_pred':>9}  {'realized_wr':>12}  {'gap':>8}  {'net':>10}")
    for r in rows:
        gap = (r['realized_wr'] or 0) - (r['avg_pred'] or 0)
        print(f"  {r['bucket']:<11}{r['n']:>4}  {r['avg_pred']:>9.3f}  "
              f"{r['realized_wr']:>11.3f}  {gap:>+7.3f}  ${r['net']:>+8.2f}")

    # ---- 4. Per edge bucket
    hr("4. PER EDGE BUCKET")
    rows = c.execute(f"""
        SELECT
          CASE
            WHEN edge < 0.05 THEN '<0.05'
            WHEN edge < 0.07 THEN '0.05-0.07'
            WHEN edge < 0.10 THEN '0.07-0.10'
            WHEN edge < 0.15 THEN '0.10-0.15'
            WHEN edge < 0.20 THEN '0.15-0.20'
            WHEN edge < 0.30 THEN '0.20-0.30'
            ELSE '0.30+'
          END AS bucket,
          COUNT(*) AS n,
          ROUND(AVG(model_prob), 3) AS avg_pred,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS realized_wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE {base_filter}
        GROUP BY bucket
        ORDER BY bucket
    """).fetchall()
    print(f"  {'bucket':<12}{'n':>4}  {'avg_pred':>9}  {'realized_wr':>12}  {'net':>10}")
    for r in rows:
        print(f"  {r['bucket']:<12}{r['n']:>4}  {r['avg_pred']:>9.3f}  "
              f"{r['realized_wr']:>11.3f}  ${r['net']:>+8.2f}")

    # ---- 5. City × lead — sample sizes (sparsity check)
    hr("5. CITY × LEAD-DAYS — sparsity check")
    rows = c.execute(f"""
        SELECT city,
          CAST(julianday(target_date) - julianday(date(created_at)) AS INT) AS lead_d,
          COUNT(*) AS n,
          ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 3) AS wr,
          ROUND(SUM(pnl), 2) AS net
        FROM trade_history
        WHERE {base_filter}
        GROUP BY city, lead_d
        HAVING n >= 3
        ORDER BY net
    """).fetchall()
    print(f"  Cells with n>=3: {len(rows)}")
    print(f"  {'city':<14}{'lead_d':>6}  {'n':>4}  {'wr':>5}  {'net':>10}")
    for r in rows:
        print(f"  {r['city']:<14}{r['lead_d']:>6}  {r['n']:>4}  "
              f"{r['wr']:>5.3f}  ${r['net']:>+8.2f}")

    # ---- 6. Existing calibration assets — what files already exist?
    hr("6. EXISTING CALIBRATION ASSETS (already in repo)")
    files = [
        "data/weather/isotonic_calibration.json",
        "data/weather/no_isotonic_calibration.json",
        "data/weather/reliability.json",
        "data/weather/per_city_ece.json",
        "data/weather/bucket_blocklist.json",
    ]
    for f in files:
        p = Path("/home/ubuntu/polymarket") / f
        if p.exists():
            print(f"  EXISTS  {f}  ({p.stat().st_size} bytes, "
                  f"mtime={p.stat().st_mtime:.0f})")
        else:
            print(f"  MISSING {f}")

    c.close()


if __name__ == "__main__":
    main()

"""Per-city walk-forward calibration analysis.

Reads tools/walk_forward/last_run_24h.csv (34K predictions, 22 cities)
and computes per-city:
  - mean predicted_prob
  - mean realized outcome (= empirical win rate)
  - calibration gap (= realized - predicted)
  - Brier score
  - Sample size
  - Bracket-band breakdown

Then compares to trade history per-city tier classifications.

NOTE: existing CSV mixes real-forecast (D-90) and reanalysis (D-91 to D-365)
training data. Not perfectly aligned to "D-90 only" but still measures
per-city bias TENDENCY at much larger n than trade history.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CSV_PATH = Path("/home/ubuntu/polymarket/tools/walk_forward/last_run_24h.csv")


def load_csv():
    rows = []
    with open(CSV_PATH) as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "city": r["city"],
                    "model": r["model"],
                    "bracket": r["bracket"],
                    "pred": float(r["predicted_prob"]),
                    "outcome": int(r["outcome"]),
                    "brier": float(r["brier"]),
                })
            except (ValueError, KeyError):
                continue
    return rows


def fmt_signed(v):
    if v is None:
        return "—"
    s = "+" if v >= 0 else "−"
    return f"{s}{abs(v):.3f}"


def main():
    print("Loading walk-forward CSV...")
    rows = load_csv()
    print(f"Loaded {len(rows)} predictions")

    # Distribution of predicted prob — sanity
    print("\n=== Distribution of predicted_prob ===")
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
    counts = [0] * (len(bins) - 1)
    for r in rows:
        p = r["pred"]
        for i in range(len(bins) - 1):
            if bins[i] <= p < bins[i+1]:
                counts[i] += 1
                break
    for i, n in enumerate(counts):
        print(f"  [{bins[i]:.1f}, {bins[i+1]:.1f}): {n:>5}")

    # Restrict to "tradable" predictions: predicted_prob in [0.55, 0.95]
    # (matches strategy.py edge() gate that requires market_prob 0.15-0.75
    # and model_prob >= 0.55. Roughly the band our strategy enters.)
    tradable = [r for r in rows if 0.55 <= r["pred"] <= 0.95]
    print(f"\nTradable predictions (0.55 <= pred <= 0.95): {len(tradable)}")

    # Per-city calibration on tradable
    print("\n=== PER-CITY CALIBRATION (tradable subset) ===")
    by_city = defaultdict(list)
    for r in tradable:
        by_city[r["city"]].append(r)

    print(f"  {'city':<14} {'n':>5}  {'mean_pred':>10}  {'mean_real':>10}  "
          f"{'gap':>9}  {'brier':>7}")
    print(f"  {'-'*14} {'-'*5}  {'-'*10}  {'-'*10}  {'-'*9}  {'-'*7}")

    city_results = []
    for city in sorted(by_city.keys()):
        sub = by_city[city]
        n = len(sub)
        mean_pred = sum(r["pred"] for r in sub) / n
        mean_real = sum(r["outcome"] for r in sub) / n
        gap = mean_real - mean_pred
        brier = sum(r["brier"] for r in sub) / n
        city_results.append((city, n, mean_pred, mean_real, gap, brier))
        print(f"  {city:<14} {n:>5}  {mean_pred:>10.3f}  {mean_real:>10.3f}  "
              f"{fmt_signed(gap):>9}  {brier:>7.4f}")

    # Sort by gap
    print("\n=== SORTED BY GAP (most under-predict to most over-predict) ===")
    print(f"  {'city':<14} {'n':>5}  {'gap':>10}  {'mean_real':>10}")
    print(f"  {'-'*14} {'-'*5}  {'-'*10}  {'-'*10}")
    for city, n, mp, mr, gap, br in sorted(city_results, key=lambda x: -x[4]):
        marker = ""
        if gap > 0.05:
            marker = "  [GREEN under-predict, lean in]"
        elif gap < -0.05:
            marker = "  [RED over-predict, downsize]"
        else:
            marker = "  [YELLOW well-calibrated]"
        print(f"  {city:<14} {n:>5}  {fmt_signed(gap):>10}  {mr:>10.3f}{marker}")

    # Compare against trade history tier from previous report
    print("\n=== TRADE-HISTORY vs WALK-FORWARD COMPARISON ===")
    th_tier = {
        "seoul":      ("A", +0.41, 6),
        "toronto":    ("A", +0.36, 5),
        "hong_kong":  ("A", +0.28, 4),
        "amsterdam":  ("A-", +0.18, 7),
        "sao_paulo":  ("B", +0.02, 5),
        "milan":      ("B", +0.03, 4),
        "wellington": ("B", +0.05, 5),
        "shanghai":   ("B", -0.04, 4),
        "buenos_aires": ("B", +0.03, 7),
        "munich":     ("C", -0.11, 7),
        "london":     ("D", -0.23, 6),
        "tokyo":      ("D", -0.31, 6),
    }

    print(f"  {'city':<14} {'TH tier':<8} {'TH gap':>8} {'TH n':>5}  "
          f"{'WF gap':>8} {'WF n':>6}  {'agree?':<8}")
    print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*5}  {'-'*8} {'-'*6}  {'-'*8}")
    wf_by_city = {city: (n, mp, mr, gap, br) for city, n, mp, mr, gap, br in city_results}
    for city, (tier, th_gap, th_n) in th_tier.items():
        if city not in wf_by_city:
            print(f"  {city:<14} {tier:<8} {fmt_signed(th_gap):>8} {th_n:>5}  "
                  f"{'—':>8} {'—':>6}  no_wf_data")
            continue
        n, mp, mr, gap, br = wf_by_city[city]
        # Agreement: signs match (both positive or both negative)
        if th_gap * gap > 0:
            agree = "YES"
        elif abs(th_gap) < 0.05 or abs(gap) < 0.05:
            agree = "weak"
        else:
            agree = "NO"
        print(f"  {city:<14} {tier:<8} {fmt_signed(th_gap):>8} {th_n:>5}  "
              f"{fmt_signed(gap):>8} {n:>6}  {agree:<8}")

    # Calibration curve per city (top 6 cities by sample size)
    print("\n=== CALIBRATION CURVE per city (sample binning) ===")
    print("  Bin = predicted_prob bucket; cell = realized win rate (n)")
    buckets = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95)]
    for city, n_total, _, _, _, _ in sorted(city_results, key=lambda x: -x[1])[:14]:
        if city not in by_city:
            continue
        sub = by_city[city]
        cells = []
        for lo, hi in buckets:
            chunk = [r for r in sub if lo <= r["pred"] < hi]
            if chunk:
                wr = sum(r["outcome"] for r in chunk) / len(chunk)
                cells.append(f"{wr:.2f} (n={len(chunk)})")
            else:
                cells.append("— (n=0)")
        print(f"  {city:<14}  pred[0.55-0.65]={cells[0]:<14}  "
              f"[0.65-0.75]={cells[1]:<14}  [0.75-0.85]={cells[2]:<14}  "
              f"[0.85-0.95]={cells[3]}")


if __name__ == "__main__":
    main()

"""Strategy-aligned per-city walk-forward analysis.

Filters the existing last_run_24h.csv to predictions that match our
LIVE TAIL-NO strategy band:

  1. Narrow brackets only (pm_1F = ±1°F symmetric ≈ 2°F wide ≈ 1.1°C wide)
     ← closest to live Polymarket EU/Asia bracket geometry (1°C ≈ 1.8°F)

  2. NO-side band: predicted_prob (YES) <= 0.45 ⟺ empirical_p_no >= 0.55
     ← matches tail-NO entry: we buy NO when model says NO is likely

  3. Excludes "free P=0" or "P=1" extremes (bracket parsing degenerates):
     keep predicted_prob in [0.05, 0.45]

Per city:
  - Mean empirical_p_no (= 1 - mean(predicted_prob))
  - Mean realized_p_no (= 1 - mean(outcome))
  - Calibration gap = realized - empirical
  - Sample size

Caveat: training window is "ALL prior errors" not "D-90 only" (existing
CSV used mixed data). For exact-match D-90, re-run backtest needed.
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
                    "pred_yes": float(r["predicted_prob"]),
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
    print(f"Total predictions: {len(rows)}")

    # ============================================================
    # FILTER 1: narrow bracket only
    # ============================================================
    print("\n=== FILTER 1: bracket types ===")
    bracket_types = defaultdict(int)
    for r in rows:
        bracket_types[r["bracket"]] += 1
    for bt, n in sorted(bracket_types.items(), key=lambda x: -x[1]):
        print(f"  {bt:<14}  n={n}")

    # Narrow brackets: pm_1F (≈1.1°C wide, closest to live Polymarket EU/Asia 1°C)
    narrow = [r for r in rows if r["bracket"] == "pm_1F"]
    print(f"\nNarrow (pm_1F only): n={len(narrow)}")

    # Also include pm_2F (≈2.2°C wide, closest to Polymarket US 5°F brackets)
    moderate = [r for r in rows if r["bracket"] in ("pm_1F", "pm_2F")]
    print(f"Narrow+moderate (pm_1F+pm_2F): n={len(moderate)}")

    # ============================================================
    # FILTER 2: tail-NO band (predicted_prob YES in [0.05, 0.45])
    # ⟺ empirical_p_no in [0.55, 0.95]
    # ============================================================
    print("\n=== FILTER 2: tail-NO trading band on narrow brackets ===")
    print("(predicted_prob_YES in [0.05, 0.45], = empirical_p_no in [0.55, 0.95])")

    def tail_no_filter(subset):
        return [r for r in subset if 0.05 <= r["pred_yes"] <= 0.45]

    narrow_tail_no = tail_no_filter(narrow)
    moderate_tail_no = tail_no_filter(moderate)
    print(f"  Narrow + tail-NO band: n={len(narrow_tail_no)}")
    print(f"  Narrow+moderate + tail-NO band: n={len(moderate_tail_no)}")

    # ============================================================
    # PER-CITY ANALYSIS — narrow + tail-NO
    # ============================================================
    print("\n=== PER-CITY: narrow brackets in tail-NO band (strategy-aligned) ===")
    by_city = defaultdict(list)
    for r in narrow_tail_no:
        by_city[r["city"]].append(r)

    print(f"  {'city':<14} {'n':>5}  {'emp_p_no':>9}  {'real_p_no':>10}  "
          f"{'gap':>9}  {'flag'}")
    print(f"  {'-'*14} {'-'*5}  {'-'*9}  {'-'*10}  {'-'*9}  {'-'*8}")

    city_results = []
    for city in sorted(by_city.keys()):
        sub = by_city[city]
        n = len(sub)
        if n < 5:
            continue
        # empirical P(NO win) = 1 - predicted_prob_yes
        # realized P(NO win) = 1 - outcome
        emp_p_no = sum(1 - r["pred_yes"] for r in sub) / n
        real_p_no = sum(1 - r["outcome"] for r in sub) / n
        gap = real_p_no - emp_p_no
        flag = ""
        if gap > 0.05:
            flag = "[GREEN model under-predicts NO]"
        elif gap < -0.05:
            flag = "[RED model over-predicts NO]"
        else:
            flag = "[YELLOW well-calibrated]"
        city_results.append((city, n, emp_p_no, real_p_no, gap))
        print(f"  {city:<14} {n:>5}  {emp_p_no:>9.3f}  {real_p_no:>10.3f}  "
              f"{fmt_signed(gap):>9}  {flag}")

    # Sort by gap
    print("\n=== SORTED BY GAP (narrow + tail-NO band) ===")
    print(f"  {'city':<14} {'n':>5}  {'gap':>9}  {'real_p_no':>10}")
    for city, n, emp, real, gap in sorted(city_results, key=lambda x: -x[4]):
        print(f"  {city:<14} {n:>5}  {fmt_signed(gap):>9}  {real:>10.3f}")

    # ============================================================
    # COMPARE to trade history tier
    # ============================================================
    print("\n=== TRADE-HISTORY tier vs WALK-FORWARD strategy-aligned gap ===")
    th_tier = {
        "seoul":      ("A",  +0.41, 6),
        "toronto":    ("A",  +0.36, 5),
        "hong_kong":  ("A",  +0.28, 4),
        "amsterdam":  ("A-", +0.18, 7),
        "buenos_aires": ("B", +0.03, 7),
        "milan":      ("B",  +0.03, 4),
        "wellington": ("B",  +0.05, 5),
        "shanghai":   ("B",  -0.04, 4),
        "sao_paulo":  ("B",  +0.02, 5),
        "munich":     ("C",  -0.11, 7),
        "london":     ("D",  -0.23, 6),
        "tokyo":      ("D",  -0.31, 6),
    }
    wf_by_city = {city: (n, emp, real, gap) for city, n, emp, real, gap in city_results}

    print(f"  {'city':<14} {'TH tier':<8} {'TH gap':>8} {'TH n':>5}   "
          f"{'WF gap':>8} {'WF n':>5}   {'consistent?'}")
    print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*5}   {'-'*8} {'-'*5}   {'-'*12}")
    for city, (tier, th_gap, th_n) in th_tier.items():
        if city not in wf_by_city:
            print(f"  {city:<14} {tier:<8} {fmt_signed(th_gap):>8} {th_n:>5}   "
                  f"{'—':>8} {'—':>5}   no_data")
            continue
        n, emp, real, wf_gap = wf_by_city[city]
        if th_gap * wf_gap > 0:
            cons = "YES (same sign)"
        elif abs(th_gap) < 0.05 or abs(wf_gap) < 0.05:
            cons = "WEAK (one ~0)"
        else:
            cons = "NO (opposite)"
        print(f"  {city:<14} {tier:<8} {fmt_signed(th_gap):>8} {th_n:>5}   "
              f"{fmt_signed(wf_gap):>8} {n:>5}   {cons}")

    # ============================================================
    # Calibration curve at tighter probability slices
    # ============================================================
    print("\n=== CALIBRATION CURVE per city (narrow + tail-NO, by p_no bucket) ===")
    p_no_buckets = [(0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.95)]
    for city in sorted(by_city.keys()):
        sub = by_city[city]
        if len(sub) < 5:
            continue
        cells = []
        for lo, hi in p_no_buckets:
            chunk = [r for r in sub if lo <= (1 - r["pred_yes"]) < hi]
            if chunk:
                wr = sum(1 - r["outcome"] for r in chunk) / len(chunk)
                cells.append(f"{wr:.2f} (n={len(chunk)})")
            else:
                cells.append("—")
        print(f"  {city:<14}  p_no[0.55-0.65]={cells[0]:<13}  "
              f"[0.65-0.75]={cells[1]:<13}  [0.75-0.85]={cells[2]:<13}  "
              f"[0.85-0.95]={cells[3]}")

    # ============================================================
    # Summary stats
    # ============================================================
    print("\n=== OVERALL ===")
    if narrow_tail_no:
        n = len(narrow_tail_no)
        avg_emp_p_no = sum(1 - r["pred_yes"] for r in narrow_tail_no) / n
        avg_real_p_no = sum(1 - r["outcome"] for r in narrow_tail_no) / n
        gap = avg_real_p_no - avg_emp_p_no
        print(f"  Narrow+tail-NO band overall:")
        print(f"  n={n}, avg empirical p_no={avg_emp_p_no:.3f}, "
              f"avg realized p_no={avg_real_p_no:.3f}, gap={gap:+.3f}")


if __name__ == "__main__":
    main()

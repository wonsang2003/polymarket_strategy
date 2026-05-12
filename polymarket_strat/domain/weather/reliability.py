"""Per-city reliability index for position-sizing shrinkage.

Motivation (Apr 24 2026 — Tier 2b of the Apr 24 dev plan, operationalizes
the §15.3.1 "reliability-weighted sizing" concept):
  The binary `_BLOCKED_CITIES` blocklist in strategy.py is a blunt instrument
  — either we trade a city at full Kelly or not at all. External review
  (polymarket analysis.pdf) recommended a smooth shrinkage formula:

      size_multiplier = min(1.0, 0.12 / city_brier)
                      × min(1.0, n_samples / 50)

  Intuition:
    * 0.12 is roughly our Tier-A Brier target (Wellington, the best city).
      A city with Brier 0.12 gets full sizing. A city at Brier 0.18 (LA
      before blocklist) gets 0.67x sizing — we still trade it when signals
      appear, just smaller. Binary blocklist is still retained as a hard
      veto for structurally-broken cities (marine-layer, bracket artifacts)
      — see _BLOCKED_CITIES in strategy.py.
    * n_samples/50 down-weights cities where we haven't accumulated enough
      real-forecast data. A city with 25 real-forecast samples gets 0.5x
      sizing even if its small-sample Brier looks great (because that
      Brier estimate itself has ±40% CI at n=25).

Data source: data/weather/reliability.json, produced by
scripts/fit_reliability.py. The file is regenerated nightly by cron after
walk-forward and after each calibration run. Graceful fallback to
multiplier=1.0 when the JSON is missing (no behavioral change from
pre-reliability code).
"""
from __future__ import annotations

import json
import os
from typing import Any


_DEFAULT_RELIABILITY_JSON = "data/weather/reliability.json"
_reliability_cache: "CityReliability | None" = None


class CityReliability:
    """Lookup per-(city, lead) Brier score + sample size, compute sizing multiplier.

    Thread-safe: reads-only after __init__.
    Fail-safe: missing / malformed JSON → multiplier=1.0 (identity).
    """

    def __init__(self, json_path: str | None = None):
        self.json_path = json_path or _DEFAULT_RELIABILITY_JSON
        # _data[city][lead_str] = {"brier": float, "n_samples": int}
        self._data: dict[str, dict[str, dict[str, float]]] = {}
        self._loaded = False
        self._note: str = ""
        self._try_load()

    def _try_load(self) -> None:
        if not os.path.exists(self.json_path):
            self._note = f"no reliability file at {self.json_path}"
            return
        try:
            with open(self.json_path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            self._note = f"failed to load {self.json_path}: {type(e).__name__}"
            return

        for city, per_lead in (payload.get("per_city", {}) or {}).items():
            validated: dict[str, dict[str, float]] = {}
            for lead_str, stats in (per_lead or {}).items():
                try:
                    brier = float(stats.get("brier", 0.0))
                    n_samples = int(stats.get("n_samples", 0))
                    if brier > 0 and n_samples >= 0:
                        entry: dict[str, float] = {
                            "brier": brier,
                            "n_samples": float(n_samples),
                        }
                        # Apr 25 2026 — augmented by scripts/compute_per_city_ece.py
                        # with honest-OOS ECE. Optional: existing files without ECE
                        # gracefully fall back to brier-only multiplier.
                        if "ece_shrunk" in stats:
                            try:
                                entry["ece_shrunk"] = float(stats["ece_shrunk"])
                            except (TypeError, ValueError):
                                pass
                        if "n_test" in stats:
                            try:
                                entry["n_test"] = float(stats["n_test"])
                            except (TypeError, ValueError):
                                pass
                        validated[str(lead_str)] = entry
                except (TypeError, ValueError):
                    continue
            if validated:
                self._data[city] = validated

        self._loaded = True
        self._note = f"loaded reliability for {len(self._data)} cities"

    def multiplier(
        self,
        *,
        city: str,
        lead_hours: int,
        brier_target: float = 0.12,
        min_samples_for_full_size: int = 50,
        ece_beta: float = 1.5,
        ece_floor: float = 0.40,
    ) -> tuple[float, dict[str, Any]]:
        """Compute the sizing multiplier for a (city, lead).

        Returns:
            (multiplier, diagnostic_dict). The dict exposes brier,
            n_samples, brier_mult, samples_mult, ece_shrunk, ece_mult,
            and fallback flags for dashboard visibility. Multiplier is
            in [0, 1].

        Formula:
            brier_mult   = min(1.0, brier_target / brier)
            samples_mult = min(1.0, n_samples / min_samples_for_full_size)
            ece_mult     = max(ece_floor, 1.0 - ece_beta * ece_shrunk)
            multiplier   = brier_mult * samples_mult * ece_mult

        ece_mult is 1.0 when ece_shrunk data is unavailable for this
        (city, lead) — backward compat with reliability.json files that
        predate compute_per_city_ece.py.

        Effective on representative cities at default params (β=1.5,
        floor=0.40) with the Apr 25 2026 honest_ece numbers (4.45%
        aggregate prior, k=50 shrinkage):
            London (ece_shrunk≈0.089) → ece_mult=0.867
            Tokyo  (ece_shrunk≈0.083) → ece_mult=0.875
            NYC    (ece_shrunk≈0.126) → ece_mult=0.811
            Mexico City (ece_shrunk≈0.155) → ece_mult=0.768
            Hypothetical 30% city (ece_shrunk≈0.20) → ece_mult=0.700
            Severe miscal (ece_shrunk≥0.40) → floored at 0.40

        If no reliability data for (city, lead), falls back to the
        per-city entry for the other lead (either lead is better than
        identity — calibration is usually correlated across leads).
        If neither lead has data, returns (1.0, fallback=True) — NO
        shrinkage, so pre-reliability code behavior is preserved.
        """
        if not self._loaded:
            return 1.0, {"fallback": True, "reason": "no_file"}

        per_lead = self._data.get(city)
        if per_lead is None:
            return 1.0, {"fallback": True, "reason": "unknown_city"}

        # Bucket the lead to 24/48 the same way isotonic does.
        bucket = "24" if lead_hours < 36 else "48"
        stats = per_lead.get(bucket)
        if stats is None:
            # Fall back to the other lead rather than identity — better
            # than nothing and Brier is correlated across leads.
            other = "48" if bucket == "24" else "24"
            stats = per_lead.get(other)
            if stats is None:
                return 1.0, {"fallback": True, "reason": "unknown_lead"}

        brier = stats["brier"]
        n = stats["n_samples"]
        brier_mult = min(1.0, brier_target / brier) if brier > 0 else 1.0
        samples_mult = min(1.0, n / float(max(min_samples_for_full_size, 1)))

        # Apr 25 2026 — ECE multiplier from honest OOS calibration.
        # Falls back to 1.0 if ece_shrunk not present (backward compat).
        ece_shrunk_v = stats.get("ece_shrunk")
        if ece_shrunk_v is None:
            ece_mult = 1.0
            ece_diag: Any = None
        else:
            ece_mult = max(ece_floor, 1.0 - ece_beta * float(ece_shrunk_v))
            ece_mult = max(0.0, min(1.0, ece_mult))
            ece_diag = round(float(ece_shrunk_v), 4)

        mult = brier_mult * samples_mult * ece_mult
        # Clamp for numerical safety
        mult = max(0.0, min(1.0, mult))

        return mult, {
            "brier": brier,
            "n_samples": n,
            "brier_mult": round(brier_mult, 4),
            "samples_mult": round(samples_mult, 4),
            "ece_shrunk": ece_diag,
            "ece_mult": round(ece_mult, 4),
            "fallback": False,
        }

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def diagnostic(self) -> str:
        return self._note


def get_city_reliability() -> CityReliability:
    """Lazy module-level singleton. First call loads the JSON, subsequent
    calls are cheap."""
    global _reliability_cache
    if _reliability_cache is None:
        _reliability_cache = CityReliability()
    return _reliability_cache


def reset_reliability_cache_for_tests() -> None:
    global _reliability_cache
    _reliability_cache = None


# -----------------------------------------------------------------------------
# Bucket-level EV blocklist — Apr 24 2026 Tier 2c.
#
# Produced by scripts/fit_bucket_blocklist.py (nightly cron). Reads
# trade_history, finds (city, lead, regime) or (city, lead) buckets with
# n_trades >= N_MIN and realized EV < 0, writes them to
# data/weather/blocked_buckets.json. strategy.analyze() loads this at
# start of cycle and short-circuits any contract matching a blocked bucket.
#
# This is the data-driven operational complement to the hard _BLOCKED_CITIES
# frozenset:
#   - _BLOCKED_CITIES is manual, based on structural model flaws (marine
#     layer, bracket-artifacts) — slow-moving, rarely updated.
#   - bucket_blocklist is automatic, refit nightly from live paper P&L —
#     fast-moving, catches deterioration as it happens.
# -----------------------------------------------------------------------------

_DEFAULT_BUCKET_BLOCKLIST_JSON = "data/weather/blocked_buckets.json"
_bucket_blocklist_cache: "BucketBlocklist | None" = None


class BucketBlocklist:
    """Runtime check: is this (city, lead, regime) bucket blocked?

    Lookup order:
      1. Exact (city, lead, regime) match in blocked_fine → blocked.
      2. (city, lead) match in blocked_coarse → blocked.
      3. Neither → not blocked.

    Graceful fallback: no JSON → no blocks (empty blocklist). New deploys
    with no trade history yet behave exactly as pre-blocklist code.
    """

    def __init__(self, json_path: str | None = None):
        self.json_path = json_path or _DEFAULT_BUCKET_BLOCKLIST_JSON
        self._fine: set[tuple[str, int, str]] = set()
        self._coarse: set[tuple[str, int]] = set()
        self._loaded = False
        self._note: str = ""
        self._try_load()

    def _try_load(self) -> None:
        if not os.path.exists(self.json_path):
            self._note = f"no blocklist file at {self.json_path}"
            return
        try:
            with open(self.json_path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            self._note = f"failed to load {self.json_path}: {type(e).__name__}"
            return

        for entry in payload.get("blocked_fine", []) or []:
            try:
                self._fine.add(
                    (str(entry["city"]), int(entry["lead_hours"]), str(entry["regime"]))
                )
            except (KeyError, TypeError, ValueError):
                continue

        for entry in payload.get("blocked_coarse", []) or []:
            try:
                self._coarse.add((str(entry["city"]), int(entry["lead_hours"])))
            except (KeyError, TypeError, ValueError):
                continue

        self._loaded = True
        self._note = (
            f"loaded {len(self._fine)} fine + {len(self._coarse)} coarse blocks"
        )

    def is_blocked(self, *, city: str, lead_hours: int, regime: str) -> tuple[bool, str]:
        """Return (blocked, reason). Reason is a short string identifying
        which blocklist tier matched, or empty if not blocked."""
        if not self._loaded:
            return False, ""

        # Bucket the lead the same way inference does
        bucket = 24 if lead_hours < 36 else 48

        # Fine match first (most specific)
        if (city, bucket, regime) in self._fine:
            return True, f"fine:{city}/{bucket}h/{regime}"

        # Coarse match
        if (city, bucket) in self._coarse:
            return True, f"coarse:{city}/{bucket}h"

        return False, ""

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def diagnostic(self) -> str:
        return self._note


def get_bucket_blocklist() -> BucketBlocklist:
    """Lazy module-level singleton. First call loads the JSON."""
    global _bucket_blocklist_cache
    if _bucket_blocklist_cache is None:
        _bucket_blocklist_cache = BucketBlocklist()
    return _bucket_blocklist_cache


def reset_bucket_blocklist_cache_for_tests() -> None:
    global _bucket_blocklist_cache
    _bucket_blocklist_cache = None

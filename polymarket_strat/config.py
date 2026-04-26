from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class TelegramConfig:
    bot_token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not bot_token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN in your .env file. Get one from @BotFather on Telegram.")
        if not chat_id:
            raise ValueError(
                "Set TELEGRAM_CHAT_ID in your .env file. "
                "Message your bot, then visit https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat ID."
            )
        return cls(bot_token=bot_token, chat_id=chat_id)


@dataclass(slots=True)
class AccountConfig:
    private_key: str
    funder: str
    signature_type: int = 0
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    @classmethod
    def from_env(cls) -> "AccountConfig":
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER", "").strip()
        if not private_key or not funder:
            raise ValueError(
                "Set POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER before enabling live trading."
            )
        if "YOUR_PRIVATE_KEY" in private_key or "YOUR_FUNDER_ADDRESS" in funder:
            raise ValueError("Replace placeholder values in your .env file before enabling live trading.")
        if not private_key.startswith("0x") or len(private_key) != 66:
            raise ValueError("POLYMARKET_PRIVATE_KEY must be a 32-byte hex private key starting with 0x.")
        if not funder.startswith("0x") or len(funder) != 42:
            raise ValueError("POLYMARKET_FUNDER must be a 20-byte hex wallet address starting with 0x.")

        signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        host = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com").strip()
        chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        return cls(
            private_key=private_key,
            funder=funder,
            signature_type=signature_type,
            host=host,
            chain_id=chain_id,
        )


@dataclass(slots=True)
class WeatherConfig:
    min_edge: float = 0.05
    fee_rate: float = 0.02
    quarter_kelly: bool = True
    max_positions_per_group: int = 2
    gfs_weight: float = 0.30
    ecmwf_weight: float = 0.40
    hrrr_weight: float = 0.20
    nam_weight: float = 0.10
    calibration_lookback_days: int = 365
    min_calibration_samples: int = 30
    db_path: str = "data/weather/weather.db"
    grib_cache_dir: str = "data/weather/grib"


def load_env_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(slots=True)
class TradingConstraints:
    bankroll: float = 1000.0
    max_single_trade_notional: float = 50.0
    max_market_notional: float = 100.0
    max_total_notional: float = 250.0
    max_open_positions: int = 4
    min_order_size: float = 5.0
    max_spread: float = 0.05
    min_top_book_liquidity: float = 100.0
    min_entry_price: float = 0.02   # skip sub-2¢ contracts (unrealistic fills, 50x+ leverage)
    max_entry_price: float = 0.85
    min_whale_agreement: int = 2
    min_signal_score: float = 1.0
    min_whale_win_rate: float = 0.60
    min_whale_avg_roi: float = 0.08
    max_portfolio_at_risk: float = 0.25
    max_category_notional: float = 100.0
    max_correlated_positions: int = 2
    reserve_cash_ratio: float = 0.35
    # Apr 25 2026 (LATE) — DISABLED for paper-mode data collection.
    # User decision: collect maximum trade tape during Phase 1 paper trading
    # so calibration / per-city ECE / live rebalance behavior all see real
    # Polymarket bracket outcomes across a wide range of conditions. The
    # mathematical effect of values >= 1.0 is that the drawdown comparator
    # `if drawdown >= soft_limit` never triggers (drawdown ∈ [0, 1] by
    # construction in PortfolioState.drawdown). risk.py's `drawdown_multiplier`
    # therefore stays at 1.0 always, and the hard-limit short-circuit in
    # PortfolioRiskManager is unreachable.
    # Re-enable to e.g. 0.08/0.15 before flipping to live mode.
    drawdown_soft_limit: float = 1.0
    drawdown_hard_limit: float = 1.0
    confidence_boost: float = 1.10
    liquidity_haircut: float = 0.60
    slippage_haircut_per_spread: float = 1.50
    # ------------------------------------------------------------------
    # Weather-strategy tradeability knobs (Apr 19 2026 refactor).
    #   - max_position_fraction: 5% per-position cap, floored at
    #     min_position_notional_usd ($10) so small bankrolls can still trade.
    #     Effective size = max(bankroll * 0.05, 10.0).
    #     At $500 → $25, at $1k → $50, at $5k → $250. Scales forever;
    #     consistent risk profile across bankroll sizes.
    #   - max_correlation_group_fraction: notional cap per correlation
    #     group (East Asia, US West, Western Europe, …). Bumped 8% → 15%
    #     to accommodate three 5% positions in one region.
    #   - max_daily_drawdown: locks the account for the day once today's
    #     cumulative settled P&L falls below -(fraction * bankroll).
    #     14% allows ~2.8 full-loss positions before braking, matching
    #     the new 5% per-position sizing. Enforced in main.py::run_autotrade.
    #   - min_edge_flat: forecast.py::edge() threshold. Replaces the old
    #     tiered `0.05 + max(0, (P-0.50)*0.40)` formula with a constant
    #     5¢ cutoff. Justified by walk-forward: market-band + flat edge
    #     is already sufficient to dominate Sharpe + model-prob filters.
    # ------------------------------------------------------------------
    # Apr 25 2026 — PLAN B emergency: halve per-position fraction (5% → 2.5%)
    # and reduce min notional ($10 → $5). With a $200 paper bankroll the
    # effective per-trade risk drops from $10 to $5. This caps damage if
    # any new high-p artifacts slip past the gate while we wait for
    # isotonic calibration data to mature on the surviving cities (NYC,
    # London, Tokyo, Seoul, Shanghai, HK, Atlanta, Chicago, Dubai, Sydney).
    # Reinstate to 5%/$10 once we see 2-3 days of positive cumulative
    # P&L on the post-Plan-B trade tape.
    max_position_fraction: float = 0.025
    min_position_notional_usd: float = 5.0
    max_correlation_group_fraction: float = 0.15
    # Apr 25 2026 (LATE) — DISABLED for paper-mode data collection.
    # Was 0.14 (14% × bankroll daily loss → autotrade brake). User decision:
    # paper trading should never auto-pause; we want maximum trade tape to
    # validate per-city ECE shrinkage and live rebalance behavior. Setting
    # to 1.00 means "lock the day only after losing the entire bankroll" —
    # effectively disabled. Re-enable to 0.14 before flipping live mode.
    max_daily_drawdown: float = 1.0
    min_edge_flat: float = 0.05
    # Apr 25 2026 — Layer 1 ML pricing feature flag. When True, the
    # strategy.py per-side evaluation will try the quantile regression
    # bracket pricer first and fall back to parametric only when the
    # ML model is unavailable for the given (city, lead).
    #
    # Apr 25 2026 (FINAL) — RE-ENABLED after fixing climatology leak.
    #
    # Story:
    #   1. Naive measure_ece reported 5.44% — flagged as suspicious by user.
    #   2. Methodology audit caught 3 leak sources: 1-year-only climatology
    #      (climo_mean ≈ actual obs), random train/test split, measure_ece
    #      holdout overlap with training.
    #   3. honest_ece.py with temporal split + train-only single-year climo
    #      → 8.14% (real OOS, but 1-yr climo is too noisy).
    #   4. Same with --mask-climatology → 6.92% but Brier 0.222 (model lost
    #      discriminating power, just predicting ~0.5 a lot — rejected).
    #   5. Built scripts/build_era5_climatology.py → fetched ERA5 1991-2020
    #      daily Tmax for all 22 cities (median 30 obs per city/doy cell).
    #      Climatology window doesn't overlap training data, so leak-free
    #      by construction.
    #   6. honest_ece.py with ERA5 climo → 4.45% aggregate ECE, well below
    #      7% target, BETTER than the leaky naive 5.44%. Reliability bins:
    #      [0.50,0.60) gap 0.8%, [0.60,0.70) gap 3.0%, [0.70,0.80) gap 0.8%
    #      — the previously-broken trading sweet spot is now well calibrated.
    #
    # Production climatology.json on EC2 is the ERA5 30-yr build. Quantile
    # models retrained against that climatology after the install.
    # Override-block in strategy.py falls back silently to parametric when
    # no quantile model exists.
    use_quantile_pricing: bool = True


@dataclass(slots=True)
class PortfolioState:
    cash: float
    current_equity: float
    peak_equity: float
    open_positions: dict[str, float]
    category_exposure: dict[str, float]
    category_position_counts: dict[str, int]

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max((self.peak_equity - self.current_equity) / self.peak_equity, 0.0)

    @classmethod
    def default(cls, constraints: TradingConstraints) -> "PortfolioState":
        return cls(
            cash=constraints.bankroll,
            current_equity=constraints.bankroll,
            peak_equity=constraints.bankroll,
            open_positions={},
            category_exposure={},
            category_position_counts={},
        )

    @classmethod
    def load(cls, path: str | Path, constraints: TradingConstraints) -> "PortfolioState":
        state_path = Path(path)
        if not state_path.exists():
            return cls.default(constraints)

        payload = json.loads(state_path.read_text())
        return cls(
            cash=float(payload.get("cash", constraints.bankroll)),
            current_equity=float(payload.get("current_equity", constraints.bankroll)),
            peak_equity=float(payload.get("peak_equity", constraints.bankroll)),
            open_positions={str(key): float(value) for key, value in dict(payload.get("open_positions", {})).items()},
            category_exposure={str(key): float(value) for key, value in dict(payload.get("category_exposure", {})).items()},
            category_position_counts={
                str(key): int(value) for key, value in dict(payload.get("category_position_counts", {})).items()
            },
        )

    def save(self, path: str | Path) -> None:
        state_path = Path(path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "cash": self.cash,
                    "current_equity": self.current_equity,
                    "peak_equity": self.peak_equity,
                    "open_positions": self.open_positions,
                    "category_exposure": self.category_exposure,
                    "category_position_counts": self.category_position_counts,
                },
                indent=2,
            )
        )

    def clone(self) -> "PortfolioState":
        return PortfolioState(
            cash=self.cash,
            current_equity=self.current_equity,
            peak_equity=self.peak_equity,
            open_positions=dict(self.open_positions),
            category_exposure=dict(self.category_exposure),
            category_position_counts=dict(self.category_position_counts),
        )

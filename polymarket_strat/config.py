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
    drawdown_soft_limit: float = 0.08
    drawdown_hard_limit: float = 0.15
    confidence_boost: float = 1.10
    liquidity_haircut: float = 0.60
    slippage_haircut_per_spread: float = 1.50


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

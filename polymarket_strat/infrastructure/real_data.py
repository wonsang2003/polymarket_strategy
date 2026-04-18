from __future__ import annotations

import csv
import json
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "—N/a", "(N/A)"):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace("±", "").replace("p", "")
    try:
        return float(text)
    except ValueError:
        return default


def _parse_fieldwork_end_date(value: str, default_year: int = 2022) -> date:
    cleaned = value.replace("–", "-").replace("—", "-").strip()
    if cleaned.lower() in {"nan", ""}:
        return date(default_year, 1, 1)

    month_matches = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", cleaned)
    day_matches = re.findall(r"\b(\d{1,2})\b", cleaned)
    year_matches = re.findall(r"\b(20\d{2})\b", cleaned)

    if month_matches and day_matches:
        month = month_matches[-1]
        day = day_matches[-1]
        year = year_matches[-1] if year_matches else str(default_year)
        return datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date()

    if len(cleaned.split()) == 3:
        return datetime.strptime(cleaned, "%d %b %Y").date()
    raise ValueError(f"Unsupported fieldwork date format: {value}")


def _looks_like_poll_date(value: Any) -> bool:
    text = str(value).strip()
    return bool(re.search(r"\d", text) and re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", text))


def ensure_processed_real_data() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    raw_2022 = RAW_DIR / "sk_polls_2022.html"
    if raw_2022.exists():
        processed_2022 = PROCESSED_DIR / "sk_polls_2022.csv"
        if not processed_2022.exists():
            _extract_korean_2022_polls(raw_2022, processed_2022)

    raw_2025 = RAW_DIR / "sk_polls_2025.html"
    if raw_2025.exists():
        processed_2025 = PROCESSED_DIR / "sk_polls_2025.csv"
        if not processed_2025.exists():
            _extract_korean_2025_polls(raw_2025, processed_2025)


def _extract_korean_2022_polls(html_path: Path, output_path: Path) -> None:
    import pandas as pd

    tables = pd.read_html(str(html_path))
    frames = []
    for idx in [2, 3, 4]:
        frame = tables[idx].copy()
        frame.columns = [col[0] if isinstance(col, tuple) else col for col in frame.columns]
        frame = frame.rename(
            columns={
                "Polling firm / Client": "polling_firm",
                "Fieldwork date": "fieldwork_date",
                "Sample size": "sample_size",
                "Margin of error": "margin_of_error",
                "Lee Jae-myung": "lee_pct",
                "Yoon Suk Yeol": "yoon_pct",
                "Others/ Undecided": "others_pct",
                "Lead": "lead_pct",
            }
        )
        frames.append(frame[["polling_firm", "fieldwork_date", "sample_size", "margin_of_error", "lee_pct", "yoon_pct", "others_pct", "lead_pct"]])

    combined = pd.concat(frames, ignore_index=True)
    combined = combined[combined["fieldwork_date"].map(_looks_like_poll_date)]
    combined["lee_pct"] = combined["lee_pct"].map(_to_float)
    combined["yoon_pct"] = combined["yoon_pct"].map(_to_float)
    combined["others_pct"] = combined["others_pct"].map(_to_float)
    combined["sample_size"] = combined["sample_size"].map(_to_float)
    combined["fieldwork_end_date"] = combined["fieldwork_date"].map(lambda value: _parse_fieldwork_end_date(str(value)).isoformat())
    combined = combined.sort_values("fieldwork_end_date")
    combined.to_csv(output_path, index=False)


def _extract_korean_2025_polls(html_path: Path, output_path: Path) -> None:
    import pandas as pd

    tables = pd.read_html(str(html_path))
    frame = tables[1].copy()
    frame.columns = [col[1] if isinstance(col, tuple) else col for col in frame.columns]
    frame = frame.rename(
        columns={
            "Fieldwork date": "fieldwork_date",
            "Sample size": "sample_size",
            "Margin of error": "margin_of_error",
            "Polling firm": "polling_firm",
            "Lee Jae-myung": "lee_pct",
            "Kim Moon-soo": "kim_pct",
            "Lead": "lead_pct",
        }
    )
    frame["lee_pct"] = frame["lee_pct"].map(_to_float)
    frame["kim_pct"] = frame["kim_pct"].map(_to_float)
    frame["sample_size"] = frame["sample_size"].map(_to_float)
    frame = frame[frame["fieldwork_date"].map(_looks_like_poll_date)]
    frame = frame[frame["lee_pct"] > 0]
    frame.to_csv(output_path, index=False)


@dataclass(slots=True)
class RealMispricingBacktestRow:
    date: str
    poll_probability: float
    market_probability_proxy: float
    turnout_signal: float
    momentum_7d: float
    momentum_14d: float
    actual_outcome: int


def load_real_mispricing_backtest_rows() -> list[RealMispricingBacktestRow]:
    ensure_processed_real_data()
    polls_path = PROCESSED_DIR / "sk_polls_2022.csv"
    market_path = RAW_DIR / "polymarket_sk_2022_market.json"
    if not polls_path.exists() or not market_path.exists():
        return []

    with polls_path.open() as handle:
        reader = csv.DictReader(handle)
        rows = [row for row in reader if _to_float(row.get("lee_pct")) > 0 and _to_float(row.get("yoon_pct")) > 0]

    rows.sort(key=lambda row: row["fieldwork_end_date"])
    probabilities = []
    for row in rows:
        lee = _to_float(row["lee_pct"]) / 100.0
        yoon = _to_float(row["yoon_pct"]) / 100.0
        total = max(lee + yoon, 1e-6)
        probabilities.append({"date": row["fieldwork_end_date"], "poll_probability": lee / total, "sample_size": _to_float(row["sample_size"], 1000.0)})

    result: list[RealMispricingBacktestRow] = []
    for index, item in enumerate(probabilities):
        trailing = probabilities[max(0, index - 7):index]
        prior_probability = trailing[-1]["poll_probability"] if trailing else item["poll_probability"]
        momentum_7d = item["poll_probability"] - (trailing[0]["poll_probability"] if trailing else item["poll_probability"])
        trailing_14 = probabilities[max(0, index - 14):index]
        momentum_14d = item["poll_probability"] - (trailing_14[0]["poll_probability"] if trailing_14 else item["poll_probability"])
        turnout_signal = min(max((item["sample_size"] - 1000.0) / 4000.0, -0.1), 0.1)
        result.append(
            RealMispricingBacktestRow(
                date=item["date"],
                poll_probability=item["poll_probability"],
                market_probability_proxy=prior_probability,
                turnout_signal=turnout_signal,
                momentum_7d=momentum_7d,
                momentum_14d=momentum_14d,
                actual_outcome=0,
            )
        )
    return result


def load_real_market_metadata() -> dict[str, Any] | None:
    market_path = RAW_DIR / "polymarket_sk_2022_market.json"
    if not market_path.exists():
        return None
    return json.loads(market_path.read_text())


def load_real_data_status() -> dict[str, Any]:
    ensure_processed_real_data()
    return {
        "raw_files": sorted(str(path.relative_to(ROOT)) for path in RAW_DIR.glob("*") if path.is_file()),
        "processed_files": sorted(str(path.relative_to(ROOT)) for path in PROCESSED_DIR.glob("*") if path.is_file()),
        "real_mispricing_rows": len(load_real_mispricing_backtest_rows()),
        "market_metadata_available": load_real_market_metadata() is not None,
    }

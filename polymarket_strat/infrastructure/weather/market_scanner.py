"""Discover active Polymarket weather temperature bracket contracts."""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from polymarket_strat.api import PolymarketPublicClient
from polymarket_strat.domain.weather.models import BracketContract, CITY_REGISTRY, c_to_f

# Regex patterns for bracket extraction from Polymarket question text
# Examples:
#   "Will the highest temperature in Tokyo be 23°C on April 15?"
#   "Will the highest temperature in Hong Kong be 30°C or higher on April 15?"
#   "Will the highest temperature in Atlanta be 73°F or below on April 15?"

_CITY_ALIASES: dict[str, str] = {
    # US
    "new york": "nyc", "nyc": "nyc", "new york city": "nyc",
    "chicago": "chicago",
    "toronto": "toronto",
    "miami": "miami",
    "atlanta": "atlanta",
    "los angeles": "la", "la": "la",
    "san francisco": "sf", "sf": "sf",
    "seattle": "seattle",
    # Europe
    "london": "london",
    "amsterdam": "amsterdam",
    "munich": "munich", "münchen": "munich",
    "milan": "milan", "milano": "milan",
    # East Asia
    "seoul": "seoul",
    "tokyo": "tokyo",
    "hong kong": "hong_kong", "hongkong": "hong_kong",
    "shanghai": "shanghai",
    "taipei": "taipei",
    #South Asia
    "Jarkarta": "jakarta", "jakarta": "jakarta",
    # South America
    "buenos aires": "buenos_aires",
    "são paulo": "sao_paulo", "sao paulo": "sao_paulo",
    # Latin America
    "mexico city": "mexico_city",
    # Oceania
    "wellington": "wellington",
    "sydney": "sydney",
    # Middle East
    "dubai": "dubai",
    "warsaw": "warsaw",
}

_WEATHER_KEYWORDS = frozenset([
    "temperature", "temp", "high temp", "highest temp", "degrees",
    "weather", "fahrenheit", "celsius",
])

# Single date: "on April 15", "for April 15"
_DATE_PATTERN = re.compile(
    r"(?:on|for)\s+"
    r"(?:(?P<month_name>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\s+"
    r"(?P<day>\d{1,2})"
    r"(?!\s*[-–])",  # negative lookahead: NOT followed by dash (that's a range)
    re.IGNORECASE,
)

# Multi-day range: "April 17-19", "April 13-15", "Apr 7 - Apr 14"
_DATE_RANGE_PATTERN = re.compile(
    r"(?:(?P<month1>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\s+"
    r"(?P<day1>\d{1,2})\s*[-–]\s*"
    r"(?:(?P<month2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+)?"
    r"(?P<day2>\d{1,2})",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Parses temperature value + optional modifier from a binary market question.
# Matches: "be 28°C on", "be 30°C or higher on", "be 73°F or below on"
_BINARY_TEMP_PATTERN = re.compile(
    r"\bbe\s+(?P<value>-?\d+(?:\.\d+)?)\s*°?\s*(?P<unit>[CcFf])?"
    r"(?:\s+(?P<modifier>or\s+(?:higher|above|below|lower)))?"
    r"\s+on\b",
    re.IGNORECASE,
)


def _parse_stringified(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
    return []


class WeatherMarketScanner:
    """Discover Polymarket weather bracket contracts for temperature trading."""

    def __init__(self, client: PolymarketPublicClient):
        self.client = client

    def find_weather_bracket_markets(
        self, *, page_size: int = 200, max_markets: int = 2000
    ) -> list[BracketContract]:
        """Scan active Polymarket markets for temperature bracket contracts.

        Paginates through markets using offset because weather markets are
        niche and typically not in the top 200 by 24h volume.

        Handles two market formats:
        - Binary Yes/No: bracket is embedded in the question text.
        - Multi-outcome: each outcome string encodes a temperature bracket.

        Returns one BracketContract per tradeable bracket.
        """
        all_markets: list[dict] = []
        offset = 0
        while offset < max_markets:
            try:
                batch = self.client.get_markets(
                    limit=page_size, active=True, order="volume24hr", offset=offset
                )
            except Exception as exc:
                print(f"[scanner] Warning: fetch at offset={offset} failed: {exc}", file=sys.stderr)
                break
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        contracts: list[BracketContract] = []

        for market in all_markets:
            question = str(market.get("question") or market.get("title") or "")
            lower_q = question.lower()

            # Must contain a weather keyword
            if not any(kw in lower_q for kw in _WEATHER_KEYWORDS):
                continue

            city_key = self._extract_city(lower_q)
            if not city_key:
                continue

            date_result = self._extract_date(question)
            if not date_result:
                continue
            target_date, end_date = date_result

            station = CITY_REGISTRY.get(city_key)
            if not station:
                continue

            # Skip if the daily high is already settled.
            # After 16:00 local time at the station, only trade tomorrow+.
            # Before 16:00, same-day contracts are still live.
            # For multi-day contracts, use end_date for the cutoff check.
            station_now = datetime.now(ZoneInfo(station.timezone))
            cutoff_passed = station_now.hour >= 16
            min_trade_date = station_now.date() + timedelta(days=1 if cutoff_passed else 0)
            relevant_date = end_date if end_date else target_date
            if relevant_date < min_trade_date:
                continue

            outcomes = _parse_stringified(market.get("outcomes"))
            token_ids = _parse_stringified(market.get("clobTokenIds"))
            if len(outcomes) < 2 or len(outcomes) != len(token_ids):
                continue

            market_id = str(market.get("conditionId") or market.get("id") or "")
            best_ask = float(market.get("bestAsk") or 1.0)
            best_bid = float(market.get("bestBid") or 0.0)
            spread = float(market.get("spread") or (best_ask - best_bid))
            liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)

            # Binary Yes/No market: bracket is in the question text
            normalized = [o.strip().lower() for o in outcomes]
            if normalized == ["yes", "no"]:
                bracket = self._parse_bracket_from_binary_question(
                    question, uses_celsius=station.uses_celsius
                )
                if bracket is None:
                    continue
                lower_f, upper_f = bracket

                outcome_prices = _parse_stringified(market.get("outcomePrices"))
                try:
                    market_price_yes = float(outcome_prices[0])
                except (IndexError, ValueError, TypeError):
                    market_price_yes = best_bid or 0.5

                contracts.append(BracketContract(
                    market_id=market_id,
                    question=question,
                    city=city_key,
                    target_date=target_date,
                    lower_f=lower_f,
                    upper_f=upper_f,
                    token_id_yes=token_ids[0],
                    token_id_no=token_ids[1],
                    market_price_yes=market_price_yes,
                    best_ask_yes=best_ask,
                    best_bid_yes=best_bid,
                    spread=spread,
                    liquidity=liquidity,
                    end_date=end_date,
                ))

            else:
                # Multi-outcome bracket market: each outcome encodes a temperature
                for outcome_str, token_id in zip(outcomes, token_ids):
                    bracket = self._parse_bracket_value(
                        outcome_str, uses_celsius=station.uses_celsius
                    )
                    if bracket is None:
                        continue
                    lower_f, upper_f = bracket

                    contracts.append(BracketContract(
                        market_id=market_id,
                        question=question,
                        city=city_key,
                        target_date=target_date,
                        lower_f=lower_f,
                        upper_f=upper_f,
                        token_id_yes=token_id,
                        market_price_yes=0.5,
                        best_ask_yes=best_ask,
                        best_bid_yes=best_bid,
                        spread=spread,
                        liquidity=liquidity,
                        end_date=end_date,
                    ))

        print(
            f"[scanner] Found {len(contracts)} weather bracket contracts across "
            f"{len(set(c.city for c in contracts))} cities.",
            file=sys.stderr,
        )
        return contracts

    def _extract_city(self, lower_question: str) -> str | None:
        for alias, key in _CITY_ALIASES.items():
            # Word-boundary match: prevents "la" hitting "milan", "atlanta", etc.
            if re.search(r'\b' + re.escape(alias) + r'\b', lower_question):
                return key
        return None

    def _extract_date(self, question: str) -> tuple[date, date | None] | None:
        """Extract target date(s) from question text.

        Returns:
            (start_date, end_date) where end_date is None for single-day contracts.
            Returns None if no date could be parsed.
        """
        now = datetime.utcnow()
        year = now.year

        def _resolve_year(d: date) -> date:
            if d < now.date() - timedelta(days=30):
                return date(year + 1, d.month, d.day)
            return d

        # Try multi-day range first: "April 17-19", "Apr 7 - Apr 14"
        range_match = _DATE_RANGE_PATTERN.search(question)
        if range_match:
            m1_name = range_match.group("month1").lower()[:3]
            m1 = _MONTH_MAP.get(m1_name)
            d1 = int(range_match.group("day1"))
            d2 = int(range_match.group("day2"))
            m2_raw = range_match.group("month2")
            m2 = _MONTH_MAP.get(m2_raw.lower()[:3]) if m2_raw else m1
            if m1 and m2:
                try:
                    start = _resolve_year(date(year, m1, d1))
                    end = _resolve_year(date(year, m2, d2))
                    if end >= start:
                        return (start, end)
                except ValueError:
                    pass

        # Single date: "on April 15"
        match = _DATE_PATTERN.search(question)
        if not match:
            return None
        month_name = match.group("month_name").lower()[:3]
        month = _MONTH_MAP.get(month_name)
        day = int(match.group("day"))
        if not month:
            return None
        try:
            candidate = _resolve_year(date(year, month, day))
        except ValueError:
            return None
        return (candidate, None)

    def _parse_bracket_from_binary_question(
        self, question: str, *, uses_celsius: bool
    ) -> tuple[float, float] | None:
        """Parse temperature bracket bounds from a binary Yes/No market question.

        Handles:
            "be 28°C on April 15"      → [c_to_f(28), c_to_f(29))
            "be 30°C or higher on ..." → [c_to_f(30), 200.0)
            "be 73°F or below on ..."  → (-50.0, 74.0)
        """
        m = _BINARY_TEMP_PATTERN.search(question)
        if not m:
            return None

        try:
            val = float(m.group("value"))
        except (TypeError, ValueError):
            return None

        unit = (m.group("unit") or "").upper()
        modifier = (m.group("modifier") or "").lower()

        # Explicit unit in question overrides station's uses_celsius
        is_celsius = (unit == "C") if unit else uses_celsius

        val_f = c_to_f(val) if is_celsius else val
        # 1°C = 1.8°F; use this as the exact-match bracket width
        step_f = (c_to_f(1) - c_to_f(0)) if is_celsius else 1.0

        if "higher" in modifier or "above" in modifier:
            return (val_f, 200.0)
        elif "below" in modifier or "lower" in modifier:
            return (-50.0, val_f + step_f)
        else:
            return (val_f, val_f + step_f)

    def _parse_bracket_value(
        self, outcome: str, *, uses_celsius: bool
    ) -> tuple[float, float] | None:
        """Parse a bracket value from a multi-outcome market outcome string.

        Handles formats like:
            "75"    → bracket [75, 76)
            "75-79" → bracket [75, 80)
            "22°C"  → convert to F, bracket [71.6, 73.4)
            "≥80"   → bracket [80, 200)  (open-ended upper)
            "≤60"   → bracket [-50, 61)  (open-ended lower)
        """
        text = outcome.strip().replace("°F", "").replace("°C", "").replace("°", "")

        range_match = re.match(r"(-?\d+\.?\d*)\s*[-–]\s*(-?\d+\.?\d*)", text)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if uses_celsius:
                low, high = c_to_f(low), c_to_f(high)
            return (low, high + 1.0)

        upper_match = re.match(r"[≥>]+\s*(-?\d+\.?\d*)", text) or re.match(r"(-?\d+\.?\d*)\+", text)
        if upper_match:
            low = float(upper_match.group(1))
            if uses_celsius:
                low = c_to_f(low)
            return (low, 200.0)

        lower_match = re.match(r"[≤<]+\s*(-?\d+\.?\d*)", text)
        if lower_match:
            high = float(lower_match.group(1))
            if uses_celsius:
                high = c_to_f(high)
            return (-50.0, high + 1.0)

        single_match = re.match(r"^(-?\d+\.?\d*)$", text)
        if single_match:
            val = float(single_match.group(1))
            if uses_celsius:
                val = c_to_f(val)
            return (val, val + 1.0)

        return None

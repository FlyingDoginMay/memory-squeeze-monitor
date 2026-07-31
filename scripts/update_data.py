#!/usr/bin/env python3
"""Refresh the public squeeze-monitor dataset."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests


TICKERS = ("SNDK", "SKHY", "MU", "NBIS")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"
OPTION_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/125 Safari/537.36"
    ),
    "Accept": "application/json",
}


def get_json(url: str, referer: str | None = None) -> dict[str, Any]:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def parse_option(raw: dict[str, Any], as_of: date) -> dict[str, Any] | None:
    match = OPTION_RE.match(str(raw.get("option", "")))
    if not match:
        return None
    _, expiry_text, option_type, strike_text = match.groups()
    expiry = datetime.strptime(expiry_text, "%y%m%d").date()
    return {
        "expiry": expiry.isoformat(),
        "dte": (expiry - as_of).days,
        "type": option_type,
        "strike": int(strike_text) / 1000,
        "bid": float(raw.get("bid") or 0),
        "ask": float(raw.get("ask") or 0),
        "iv": float(raw.get("iv") or 0),
        "delta": float(raw.get("delta") or 0),
        "gamma": float(raw.get("gamma") or 0),
        "oi": float(raw.get("open_interest") or 0),
        "volume": float(raw.get("volume") or 0),
    }


def is_liquid(option: dict[str, Any]) -> bool:
    if option["dte"] < 7 or option["dte"] > 75:
        return False
    if option["iv"] <= 0 or option["bid"] <= 0 or option["ask"] <= option["bid"]:
        return False
    mid = (option["bid"] + option["ask"]) / 2
    spread = (option["ask"] - option["bid"]) / mid if mid else math.inf
    return option["oi"] >= 5 and spread <= 0.60


def expiry_skews(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for expiry in sorted({option["expiry"] for option in options if is_liquid(option)}):
        chain = [
            option for option in options if option["expiry"] == expiry and is_liquid(option)
        ]
        calls = [option for option in chain if option["type"] == "C"]
        puts = [option for option in chain if option["type"] == "P"]
        if not calls or not puts:
            continue
        call = min(calls, key=lambda option: abs(option["delta"] - 0.25))
        put = min(puts, key=lambda option: abs(option["delta"] + 0.25))
        if abs(call["delta"] - 0.25) > 0.12 or abs(put["delta"] + 0.25) > 0.12:
            continue
        results.append(
            {
                "expiry": expiry,
                "dte": call["dte"],
                "skew": round((put["iv"] - call["iv"]) * 100, 3),
                "put_iv": round(put["iv"] * 100, 3),
                "call_iv": round(call["iv"] * 100, 3),
                "put_strike": put["strike"],
                "call_strike": call["strike"],
            }
        )
    return results


def interpolate_30d(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: row["dte"])
    below = [row for row in rows if row["dte"] <= 30]
    above = [row for row in rows if row["dte"] >= 30]
    if below and above:
        low, high = below[-1], above[0]
        if low["dte"] == high["dte"]:
            return {**low, "method": "nearest"}
        weight = (30 - low["dte"]) / (high["dte"] - low["dte"])
        return {
            "expiry": f'{low["expiry"]}/{high["expiry"]}',
            "dte": 30,
            "skew": round(low["skew"] + weight * (high["skew"] - low["skew"]), 3),
            "put_iv": round(
                low["put_iv"] + weight * (high["put_iv"] - low["put_iv"]), 3
            ),
            "call_iv": round(
                low["call_iv"] + weight * (high["call_iv"] - low["call_iv"]), 3
            ),
            "method": "linear_interpolation",
        }
    nearest = min(rows, key=lambda row: abs(row["dte"] - 30))
    return {**nearest, "method": "nearest"}


def call_walls(options: list[dict[str, Any]], spot: float) -> list[dict[str, Any]]:
    aggregated: dict[float, dict[str, float]] = {}
    for option in options:
        if option["type"] != "C" or option["dte"] < 0 or option["dte"] > 45:
            continue
        if option["strike"] < spot or option["strike"] > spot * 1.50:
            continue
        row = aggregated.setdefault(
            option["strike"], {"oi": 0.0, "volume": 0.0, "gamma_concentration": 0.0}
        )
        row["oi"] += option["oi"]
        row["volume"] += option["volume"]
        row["gamma_concentration"] += (
            option["gamma"] * option["oi"] * 100 * spot * spot * 0.01
        )
    rows = [
        {
            "strike": strike,
            "oi": int(values["oi"]),
            "volume": int(values["volume"]),
            "distance_pct": round((strike / spot - 1) * 100, 2),
            "gamma_concentration": round(values["gamma_concentration"], 0),
        }
        for strike, values in aggregated.items()
    ]
    return sorted(rows, key=lambda row: row["oi"], reverse=True)[:5]


def borrow(ticker: str) -> dict[str, Any]:
    payload = get_json(
        f"https://www.iborrowdesk.com/api/ticker/{ticker}",
        referer=f"https://www.iborrowdesk.com/report/{ticker}",
    )
    daily = payload["daily"]
    latest = daily[-1]
    base = daily[max(0, len(daily) - 6)]
    fee_change = float(latest["fee"]) - float(base["fee"])
    availability_change = (
        (float(latest["available"]) / float(base["available"]) - 1) * 100
        if float(base["available"])
        else None
    )
    covering = (
        "strong"
        if fee_change < -0.25 and availability_change is not None and availability_change > 20
        else "mild"
        if fee_change < 0 and availability_change is not None and availability_change > 0
        else "none"
    )
    return {
        "as_of": latest["date"],
        "fee_pct": round(float(latest["fee"]), 4),
        "available": int(latest["available"]),
        "fee_change_5d_pp": round(fee_change, 4),
        "available_change_5d_pct": (
            round(availability_change, 2) if availability_change is not None else None
        ),
        "covering_proxy": covering,
    }


def collect_ticker(ticker: str) -> dict[str, Any]:
    cboe = get_json(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json")
    data = cboe["data"]
    as_of = datetime.fromisoformat(cboe["timestamp"]).date()
    spot = float(data["current_price"])
    options = [
        parsed
        for raw in data["options"]
        if (parsed := parse_option(raw, as_of)) is not None
    ]
    skews = expiry_skews(options)
    return {
        "ticker": ticker,
        "market_time": cboe["timestamp"],
        "spot": round(spot, 4),
        "price_change_pct": round(float(data.get("price_change_percent") or 0), 3),
        "iv30_pct": round(float(data.get("iv30") or 0), 3),
        "skew_30d": interpolate_30d(skews),
        "skew_term_structure": skews,
        "call_walls": call_walls(options, spot),
        "borrow": borrow(ticker),
        "low_history_confidence": ticker == "SKHY",
    }


def load_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def add_trend(latest: list[dict[str, Any]], history: list[dict[str, Any]]) -> None:
    for item in latest:
        prior = next(
            (
                snapshot
                for snapshot in reversed(history)
                if snapshot["ticker"] == item["ticker"]
                and snapshot["market_time"] != item["market_time"]
                and snapshot.get("skew_30d") is not None
            ),
            None,
        )
        current = item.get("skew_30d")
        if prior and current:
            change = current["skew"] - prior["skew_30d"]["skew"]
            item["skew_change"] = round(change, 3)
            item["skew_trend"] = (
                "rebounding" if change >= 1 else "falling" if change <= -1 else "flat"
            )
        else:
            item["skew_change"] = None
            item["skew_trend"] = "building_history"


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    history = load_history()
    latest = [collect_ticker(ticker) for ticker in TICKERS]
    add_trend(latest, history)
    generated_at = datetime.now(timezone.utc).isoformat()
    market_time = max(item["market_time"] for item in latest)
    payload = {
        "generated_at": generated_at,
        "market_time": market_time,
        "refresh_interval_minutes": 30,
        "tickers": latest,
        "methodology": {
            "skew": "30-day interpolated 25-delta put IV minus call IV, volatility points.",
            "call_wall": "Largest call OI strikes above spot, 0-45 DTE, up to 150% of spot.",
            "borrow": "IBorrowDesk/IBKR indicative fee and available shares; not the whole market.",
        },
    }
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    existing_keys = {(item["ticker"], item["market_time"]) for item in history}
    for item in latest:
        key = (item["ticker"], item["market_time"])
        if key not in existing_keys:
            history.append(
                {
                    "ticker": item["ticker"],
                    "market_time": item["market_time"],
                    "spot": item["spot"],
                    "skew_30d": item["skew_30d"],
                    "call_wall": item["call_walls"][0] if item["call_walls"] else None,
                    "borrow": item["borrow"],
                }
            )
    history = history[-18000:]
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Updated {LATEST_PATH}")
    print(f"Updated {HISTORY_PATH}")


if __name__ == "__main__":
    main()

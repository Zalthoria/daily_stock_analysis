"""Daily US/SG discovery using the existing screening indicators and scorer.

This is an Actions/CLI entry point, not an expansion of DSA's supported
single-stock markets. Singapore picks stay in this quantitative report.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

from src.services.screening.daily import compute_daily_features
from src.services.screening.models import ScreeningConfig
from src.services.screening.scorer import compute_screen_scores


@dataclass(frozen=True)
class Market:
    name: str
    timezone: str
    calendar: str
    currency: str
    exchanges: tuple[str, ...]
    min_price: float
    min_cap: float
    min_volume: int
    min_turnover: float
    benchmarks: tuple[tuple[str, str], ...]


MARKETS = {
    "us": Market("United States", "America/New_York", "XNYS", "USD",
                 ("NYQ", "NMS", "NGM", "NCM", "ASE"), 5, 2e9, 300000, 1e7,
                 (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"))),
    "sg": Market("Singapore", "Asia/Singapore", "XSES", "SGD",
                 ("SES",), 0.2, 1e8, 20000, 5e5,
                 (("^STI", "Straits Times Index"),)),
}
FINAL_WEIGHTS = {"momentum": 0.5, "stability": 0.2, "liquidity": 0.2, "value": 0.1}
SEED_WEIGHTS = {"momentum": 0.4, "liquidity": 0.4, "value": 0.2}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def completed_session(market: Market, now: datetime):
    """Fail closed if the exchange calendar cannot establish a completed bar."""
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cal = xcals.get_calendar(market.calendar)
    local_day = now.astimezone(ZoneInfo(market.timezone)).date()
    # Allow a publication buffer; never compare a partial session with full bars.
    cutoff = stamp.tz_convert("UTC") - pd.Timedelta(minutes=20)
    schedule = cal.schedule.loc[str(local_day - pd.Timedelta(days=14)):str(local_day)]
    closed = schedule.loc[schedule["close"] <= cutoff]
    if closed.empty:
        raise ValueError("No completed session in the calendar window")
    target = closed.index[-1].date()
    phase = "non-trading day"
    if cal.is_session(pd.Timestamp(local_day)):
        opened = cal.session_open(pd.Timestamp(local_day))
        ended = cal.session_close(pd.Timestamp(local_day))
        phase = "premarket" if stamp < opened else "intraday" if stamp < ended else "postmarket"
    return target, phase


def snapshot_rows(quotes, market: Market, target):
    """Validate the exchange/currency and reject stale quotes and depositary receipts."""
    rows = []
    for quote in quotes:
        symbol = str(quote.get("symbol", ""))
        label = str(quote.get("shortName") or quote.get("longName") or symbol)
        stamp = number(quote.get("regularMarketTime"))
        if (quote.get("quoteType") != "EQUITY" or quote.get("currency") != market.currency
                or quote.get("exchange") not in market.exchanges
                or re.search(r"\b(SDR|ADR|warrant|leveraged|inverse)\b", label, re.I)):
            continue
        pattern = r"[A-Z0-9]{1,8}\.SI" if market.currency == "SGD" else r"[A-Z]{1,5}(?:-[A-Z])?"
        if not re.fullmatch(pattern, symbol) or stamp is None:
            continue
        quote_day = datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo(market.timezone)).date()
        if quote_day < target:
            continue
        price, cap, volume = (number(quote.get(k)) for k in
                              ("regularMarketPrice", "marketCap", "averageDailyVolume3Month"))
        if (price is None or cap is None or volume is None or price < market.min_price
                or cap < market.min_cap or volume < market.min_volume
                or price * volume < market.min_turnover):
            continue
        same_currency = quote.get("financialCurrency") in (None, market.currency)
        rows.append({"code": symbol, "name": label, "price": price, "amount": price * volume,
                     "total_mv": cap, "pe_ratio": number(quote.get("trailingPE")) if same_currency else None,
                     "pb_ratio": number(quote.get("priceToBook")) if same_currency else None,
                     "change_pct": number(quote.get("regularMarketChangePercent")),
                     "closing_quote": closing_bar(quote, market, target),
                     "quote_time": datetime.fromtimestamp(stamp, timezone.utc).isoformat()})
    return pd.DataFrame(rows).drop_duplicates("code") if rows else pd.DataFrame()


def fetch_universe(key, market, target, limit):
    q = yf.EquityQuery
    query = q("and", [q("eq", ["region", key]), q("is-in", ["exchange", *market.exchanges]),
                      q("gte", ["intradayprice", market.min_price]),
                      q("gte", ["intradaymarketcap", market.min_cap]),
                      q("gte", ["avgdailyvol3m", market.min_volume])])
    response = yf.screen(query, size=limit, sortField="intradaymarketcap", sortAsc=False)
    quotes = response.get("quotes", [])
    if not quotes:
        raise RuntimeError("Yahoo screener returned no quotes; no sample-watchlist fallback")
    return snapshot_rows(quotes, market, target), int(response.get("total", len(quotes))), len(quotes)


def closing_bar(quote, market, target):
    """Accept a same-session near-close quote only; target is already completed."""
    stamp = number(quote.get("regularMarketTime"))
    if stamp is None or quote.get("currency") != market.currency:
        return None
    quoted_at = pd.Timestamp(stamp, unit="s", tz="UTC")
    if quoted_at.tz_convert(market.timezone).date() != target:
        return None
    close_time = xcals.get_calendar(market.calendar).session_close(pd.Timestamp(target))
    if quoted_at < close_time - pd.Timedelta(minutes=15):
        return None
    bar = {k: number(quote.get(field)) for k, field in {
        "open": "regularMarketOpen", "high": "regularMarketDayHigh", "low": "regularMarketDayLow",
        "close": "regularMarketPrice", "volume": "regularMarketVolume"}.items()}
    if any(v is None for v in bar.values()):
        return None
    if (min(bar[k] for k in ("open", "high", "low", "close")) <= 0 or bar["volume"] < 0
            or bar["high"] < max(bar["open"], bar["close"])
            or bar["low"] > min(bar["open"], bar["close"])):
        return None
    return dict(bar, date=target.isoformat())


def normalized_history(data, symbol, target, closing_quote=None):
    if isinstance(data.columns, pd.MultiIndex):
        if symbol not in data.columns.get_level_values(0):
            raise ValueError("history missing")
        data = data[symbol]
    frame = data.rename(columns=str.lower).copy()
    frame["date"] = pd.to_datetime(frame.index).date
    frame = frame.loc[frame["date"] <= target].dropna(subset=["open", "high", "low", "close", "volume"])
    supplemented = False
    if closing_quote and closing_quote["date"] == target.isoformat() and target not in frame["date"].values:
        # Yahoo can publish completed OHLC quotes before its daily Close/Adj Close fields.
        # Never substitute a quote from another session or synthesize older missing bars.
        frame = pd.concat([frame.reset_index(drop=True), pd.DataFrame([dict(closing_quote, date=target)])],
                          ignore_index=True)
        supplemented = True
    frame = frame.sort_values("date").drop_duplicates("date")
    if len(frame) < 61:
        raise ValueError("fewer than 61 completed daily bars")
    if frame.iloc[-1]["date"] != target:
        raise ValueError(f"stale history: latest {frame.iloc[-1]['date']}, expected {target}")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("non-positive price in history")
    if (frame["volume"] < 0).any():
        raise ValueError("negative volume in history")
    frame = frame.reset_index(drop=True)
    frame.attrs["closing_quote_used"] = supplemented
    return frame


def rank(frame, weights):
    if frame.empty:
        return frame
    config = ScreeningConfig(factor_weights=weights)
    return compute_screen_scores(frame, config).sort_values(["screen_score", "code"], ascending=[False, True])


def enrich(row, history):
    row = dict(row)
    row.update(compute_daily_features(history))
    close = history["close"]
    row.update(price=float(close.iloc[-1]), change_pct=float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
               change_20d=float((close.iloc[-1] / close.iloc[-21] - 1) * 100),
               amount=float((history["close"] * history["volume"]).tail(20).mean()),
               volume_ratio=row.get("volume_ratio_20d"),
               bar_source="completed-session quote" if history.attrs.get("closing_quote_used") else "daily history",
               bar_date=str(history.iloc[-1]["date"]))
    return row


def eligible(row, market):
    """Require actual trend evidence, with an explicit no-candidates outcome."""
    values = [number(row.get(k)) for k in ("price", "ma20", "ma60", "change_60d", "rsi14", "amount")]
    if any(v is None for v in values):
        return False
    price, ma20, ma60, momentum, rsi, turnover = values
    return (price >= market.min_price and turnover >= market.min_turnover
            and price > ma20 >= ma60 > 0 and momentum > 0 and rsi <= 75
            and price / ma20 <= 1.10 and number(row.get("daily_quality_score")) is not None
            and row["daily_quality_score"] >= 80)


def scan_market(key, now, pool_size, history_limit, top_n):
    market = MARKETS[key]
    target, phase = completed_session(market, now)
    frame, matched, returned = fetch_universe(key, market, target, pool_size)
    initial = rank(frame, SEED_WEIGHTS).head(history_limit)
    tickers = initial["code"].tolist() if not initial.empty else []
    tickers += [symbol for symbol, _ in market.benchmarks]
    # Bounded requests, no credential requirements or private source fallback.
    data = yf.download(tickers, period="6mo", interval="1d", group_by="ticker",
                       auto_adjust=True, progress=False, threads=4, timeout=15)
    rows, excluded, benchmarks = [], [], []
    for row in initial.to_dict("records"):
        try:
            rows.append(enrich(row, normalized_history(data, row["code"], target, row.get("closing_quote"))))
        except (ValueError, KeyError, TypeError) as exc:
            excluded.append({"code": row["code"], "reason": str(exc)})
    for symbol, name in market.benchmarks:
        try:
            try:
                history = normalized_history(data, symbol, target)
            except ValueError:
                quote = closing_bar(yf.Ticker(symbol).get_info(), market, target)
                history = normalized_history(data, symbol, target, quote)
            benchmarks.append(enrich({"code": symbol, "name": name}, history))
        except Exception as exc:
            excluded.append({"code": symbol, "reason": str(exc)})
    if len(initial) and not rows:
        details = "; ".join(f"{x['code']}: {x['reason']}" for x in excluded[:3])
        raise RuntimeError(f"All candidate histories failed freshness/quality checks; screening unavailable. {details}")
    if not rows and not benchmarks:
        raise RuntimeError("No fresh completed-session histories available")
    scored = rank(pd.DataFrame(rows), FINAL_WEIGHTS)
    all_rows = scored.to_dict("records")
    qualified = [row for row in all_rows if eligible(row, market)]
    return {"market": key, "status": "ok", "phase_now": phase, "bar_date": str(target),
            "matched_universe": matched, "returned": returned, "valid_snapshots": len(frame),
            "history_attempted": len(initial), "history_valid": len(rows),
            "qualifying": len(qualified), "picks": qualified[:top_n],
            "benchmarks": benchmarks, "ranked_candidates": all_rows, "excluded": excluded}


def clean_json(value):
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if value is None or value is pd.NA:
        return None
    if hasattr(value, "item"):
        return clean_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def fmt(value, suffix=""):
    v = number(value)
    return f"{v:.2f}{suffix}" if v is not None else "unavailable"


def cell(value):
    return str(value).replace("|", "/").replace("\n", " ")


def render_report(result):
    lines = ["# Daily market discovery — US and Singapore", "",
             f"Generated: {result['generated_at']} (Singapore time)", "",
             "Automatically discovered candidates; no personal watchlist is required.", "",
             "## How to read this report", "",
             "This is a transparent trend/liquidity research screen, not a buy list. Scores are heuristic rankings, "
             "not probabilities or expected returns. Prices and trends below use adjusted, completed daily sessions. "
             "The snapshot prefilter may include delayed/intraday quotes; each selected stock is then checked against "
             "the latest completed exchange session. US and Singapore dates can differ.", "",
             "When Yahoo's latest daily close is missing, a timestamped same-session near-close OHLC quote may complete "
             "that one bar. Such substitutions are labelled below and in JSON; older missing history is never fabricated.", "",
             "Coverage is capped at the largest qualifying listings by market capitalization, not every listed stock. "
             "Within that pool, snapshot scoring selects a bounded history-check pool. Rankings are separate by market; "
             "liquidity is measured in each market's currency. This favors liquid, established companies.", "",
             "News, earnings catalysts and future returns are not verified by this screen. "
             "Check announcements, earnings dates, valuation and trading costs before acting. "
             "Yahoo data can be delayed or missing; exclusions are recorded in the accompanying JSON.", ""]
    for item in result["markets"]:
        market = MARKETS[item["market"]]
        lines += [f"## {market.name}", ""]
        if item["status"] != "ok":
            lines += [f"**Unavailable:** {cell(item['error'])}. No picks substituted.", ""]
            continue
        lines += [f"Current exchange phase: **{item['phase_now']}**. Latest completed daily session used: **{item['bar_date']}**.", "",
                  f"Source matched {item['matched_universe']} listings; fetched {item['returned']}; "
                  f"{item['valid_snapshots']} survived quote/currency/liquidity checks. "
                  f"History checked for {item['history_attempted']}, fresh histories for {item['history_valid']}; "
                  f"{item['qualifying']} met the final trend rules.", "",
                  f"Minimum price: {market.currency} {market.min_price:g}; market cap: {market.currency} {market.min_cap:,.0f}; "
                  f"3-month average volume: {market.min_volume:,} shares; "
                  f"20-session average traded value: {market.currency} {market.min_turnover:,.0f}.", "",
                  "### Market overview — completed-session data", "",
                  "| Index | Session | Close (points) | 1-session change | 20-session change | Versus 20-day average |",
                  "|---|---|---:|---:|---:|---|"]
        for b in item["benchmarks"]:
            lines.append(f"| {cell(b['name'])} | {b['bar_date']} | {fmt(b['price'])} | {fmt(b['change_pct'], '%')} | "
                         f"{fmt(b['change_20d'], '%')} | {'Above' if b['price'] > b['ma20'] else 'Below'} |")
        if not item["benchmarks"]:
            lines += ["", "Index data unavailable; no market narrative generated."]
        quoted_indices = [b["name"] for b in item["benchmarks"] if b["bar_source"] == "completed-session quote"]
        if quoted_indices:
            lines += ["", f"Latest bar completed from timestamped closing quotes: {', '.join(quoted_indices)}."]
        lines += ["", "### Automatically selected research shortlist", ""]
        if not item["picks"]:
            lines += ["**No candidates met the rules today.** The system does not force a recommendation.", ""]
        for n, row in enumerate(item["picks"], 1):
            lines += [f"#### {n}. {cell(row['name'])} ({row['code']})", "",
                      f"- Rank score: {fmt(row['screen_score'])}/100. Last completed adjusted close: {market.currency} {fmt(row['price'])} on {row['bar_date']}.",
                      f"- Why surfaced: price is above the 20-day average ({fmt(row['ma20'])}), "
                      f"which is at or above the 60-day average ({fmt(row['ma60'])}); "
                      f"60-session return is {fmt(row['change_60d'], '%')}.",
                      f"- Liquidity: 20-session average traded value {market.currency} {fmt(row['amount'] / 1e6)} million. "
                      f"Latest volume / previous 20-session average: {fmt(row.get('volume_ratio_20d'), 'x')}.",
                      f"- Stretch/risk: RSI(14) {fmt(row['rsi14'])}; distance above 20-day average "
                      f"{fmt((row['price'] / row['ma20'] - 1) * 100, '%')}; "
                      f"20-session maximum drawdown {fmt(row.get('max_drawdown_20d_pct'), '%')}.",
                      "- Follow-up: verify company announcements and upcoming earnings; reassess if price loses its 20-day average. "
                      "These checks do not establish intrinsic value or a guaranteed entry point.", ""]
            if row["bar_source"] == "completed-session quote":
                lines += [f"Latest bar completed from a closing quote timestamped {row['quote_time']}; daily close field was missing.", ""]
        if item["excluded"]:
            lines += [f"History/index exclusions: {len(item['excluded'])}; see JSON for symbols and reasons.", ""]
    lines += ["## Method and additional analysis", "",
              "Snapshot score: 40% momentum, 40% liquidity, 20% value. "
              "Final score: 50% momentum, 20% stability, 20% liquidity, 10% value, using DSA's existing scorer. "
              "Final eligibility requires price > MA20 >= MA60, positive 60-session return, RSI <= 75, "
              "price <= 110% of MA20, and daily data quality >= 80. Missing required evidence excludes a candidate.", "",
              "The selected US stocks are passed to the existing Gemini analysis pipeline for an additional report. "
              "Singapore picks receive the quantitative analysis above; they are deliberately not passed through "
              "DSA's US/China-specific single-stock routing. The shortlist may be concentrated and is not a portfolio allocation.", "",
              "Source: Yahoo Finance via yfinance. Calendars: exchange-calendars (XNYS/XSES). "
              "This report is calculated from data; no LLM-generated market recap is used."]
    return "\n".join(lines) + "\n"


def run(markets, now, pool_size=200, history_limit=30, top_n=3, scanner=scan_market):
    result = {"generated_at": now.astimezone(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds"),
              "markets": []}
    for key in markets:
        try:
            item = scanner(key, now, pool_size, history_limit, top_n)
        except Exception as exc:
            item = {"market": key, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "picks": []}
        result["markets"].append(item)
    return clean_json(result)


def main():
    markets = list(dict.fromkeys(x.strip().lower() for x in os.getenv("DISCOVERY_MARKETS", "us,sg").split(",") if x.strip()))
    if not markets or any(m not in MARKETS for m in markets):
        raise ValueError("DISCOVERY_MARKETS must contain us and/or sg")
    pool = int(os.getenv("DISCOVERY_POOL_SIZE", "200"))
    history = int(os.getenv("DISCOVERY_HISTORY_LIMIT", "30"))
    top = int(os.getenv("DISCOVERY_TOP_N", "3"))
    if not (1 <= top <= 5 and top <= history <= pool <= 250):
        raise ValueError("Require 1 <= top <= 5 and top <= history <= pool <= 250")
    result = run(markets, datetime.now(timezone.utc), pool, history, top)
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    (reports / "market_discovery.md").write_text(render_report(result), encoding="utf-8")
    (reports / "market_discovery.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    stocks = [p["code"] for m in result["markets"] if m["market"] == "us" and m["status"] == "ok" for p in m["picks"]]
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"stock_list={','.join(stocks)}\n")
    for item in result["markets"]:
        print(f"{item['market']}: {item['status']}; selected {[p['code'] for p in item['picks']]}")
        if item["status"] != "ok":
            print(item["error"])
    return 0 if any(m["status"] == "ok" for m in result["markets"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

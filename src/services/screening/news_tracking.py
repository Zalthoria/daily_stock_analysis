"""Prospective signal journal: next-session open, completed-session outcomes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

from src.services.screening.news_intelligence import VERSION, stamp


def observe(record, history, calendar, cutoff):
    observed = pd.Timestamp(stamp(record["recorded_at"]))
    first = calendar.minute_to_session(observed.floor("min"), direction="next")
    if calendar.session_open(first) <= observed:
        first = calendar.next_session(first)
    frame = history.rename(columns=str.lower).dropna(subset=["open", "close"]).sort_index()
    rows = []
    for index, row in frame.iterrows():
        day = pd.Timestamp(index).date()
        if (calendar.is_session(day) and calendar.session_open(day) > observed
                and calendar.session_close(day) <= pd.Timestamp(cutoff) and row["open"] > 0 and row["close"] > 0):
            rows.append((day, float(row["open"]), float(row["close"])))
    if not rows:
        return {"status": "unavailable", "reason": "Missing completed entry session"} if calendar.session_close(first) <= pd.Timestamp(cutoff) else {"status": "pending", "outcomes": {}}
    expected = [x.date() for x in calendar.sessions_in_range(first, rows[-1][0])]
    if [r[0] for r in rows] != expected:
        return {"status": "unavailable", "reason": "Missing entry or intermediate session; entry was not shifted"}
    entry, outcomes = rows[0][1], {}
    for horizon in (1, 5, 20):
        if len(rows) >= horizon:
            gross = (rows[horizon - 1][2] / entry - 1) * 100
            outcomes[str(horizon)] = {"gross_pct": gross, "net_pct": gross - record["cost_bps"] / 100,
                                     "worst_close_from_entry_pct": min(0, min(r[2] for r in rows[:horizon]) / entry * 100 - 100)}
    return {"status": "complete" if len(rows) >= 20 else "tracking", "entry_date": str(rows[0][0]),
            "entry_price": entry, "outcomes": outcomes}


def update_tracking(result, now, directory, download=yf.download):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path, records, errors = directory / "signals.json", [], []
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list) or any(not isinstance(r, dict) or not stamp(r.get("recorded_at")) or
                                                   r.get("market") not in {"us", "sg"} or not r.get("id") or not r.get("code")
                                                   for r in records):
                raise ValueError("Invalid journal structure")
        except (ValueError, TypeError):
            path.rename(directory / f"signals-corrupt-{now.strftime('%Y%m%dT%H%M%S%f')}.json")
            records = []
            errors.append("Invalid previous journal preserved separately; a new tracking period started")
    else:
        errors.append("No previous journal restored; tracking starts with this run")
    for record in records:
        if (now - stamp(record["recorded_at"])).days > 85 and record.get("observation", {}).get("status") != "complete":
            record["observation"] = {"status": "expired_unavailable", "reason": "Outside bounded history window; manual follow-up required"}
    for market, calendar_name in (("us", "XNYS"), ("sg", "XSES")):
        pending = [r for r in records if r["market"] == market and r.get("observation", {}).get("status") not in {"complete", "expired_unavailable"}]
        symbols = sorted({r["code"] for r in pending})[:150]
        if not symbols:
            continue
        try:
            history = download(symbols, period="3mo", interval="1d", auto_adjust=True,
                               group_by="ticker", progress=False, threads=4, timeout=15)
            calendar = xcals.get_calendar(calendar_name)
            for record in pending:
                if record["code"] not in symbols:
                    continue
                try:
                    frame = history[record["code"]] if isinstance(history.columns, pd.MultiIndex) else history
                    record["observation"] = observe(record, frame, calendar, now)
                except (ValueError, KeyError, TypeError) as exc:
                    record["observation"] = {"status": "unavailable", "reason": str(exc)[:180]}
        except Exception as exc:
            errors.append(f"{market} outcome update unavailable: {type(exc).__name__}")
    costs = int(os.getenv("NEWS_ROUND_TRIP_COST_BPS", "20"))
    if not 0 <= costs <= 1000:
        raise ValueError("NEWS_ROUND_TRIP_COST_BPS must be 0..1000")
    known = {r["id"] for r in records}
    for market in result["markets"]:
        if market["status"] != "ok":
            continue
        cohorts = {"price_only": market["picks"],
                   "news_confirmed": [r for r in market.get("news_rankings", []) if r["status"] == "candidate"][:3]}
        for cohort, rows in cohorts.items():
            for row in rows:
                ident = hashlib.sha256(f"{now.date()}:{market['market']}:{row['code']}:{cohort}:{VERSION}".encode()).hexdigest()[:24]
                if ident in known:
                    continue
                known.add(ident)
                records.append({"id": ident, "version": VERSION, "market": market["market"], "code": row["code"],
                                "cohort": cohort, "recorded_at": now.isoformat(), "score": row.get("score", row.get("screen_score")),
                                "cost_bps": costs, "news_ids": [x["story"]["id"] for x in row.get("events", [])],
                                "observation": {"status": "pending", "outcomes": {}}})
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    lines = ["## Forward tracking", "", f"Journal contains {len(records)} dated signals. "
             "Entry is the first exchange open strictly after the signal was recorded; returns use completed adjusted closes. "
             f"Assumed round-trip cost for new observations: {costs} bps. Missing entry/intermediate bars remain unavailable.", "",
             "| Market | Cohort | Sessions | Observations | Mean net return | Worst observed close vs entry |",
             "|---|---|---:|---:|---:|---:|"]
    for market in ("us", "sg"):
        for cohort in ("price_only", "news_confirmed"):
            for horizon in ("1", "5", "20"):
                outcomes = [r["observation"]["outcomes"][horizon] for r in records if r["market"] == market and r["cohort"] == cohort
                            and horizon in r.get("observation", {}).get("outcomes", {})]
                if outcomes:
                    lines.append(f"| {market} | {cohort} | {horizon} | {len(outcomes)} | "
                                 f"{sum(x['net_pct'] for x in outcomes) / len(outcomes):+.2f}% | "
                                 f"{min(x['worst_close_from_entry_pct'] for x in outcomes):+.2f}% |")
    states = {}
    for record in records:
        state = record.get("observation", {}).get("status", "unknown")
        states[state] = states.get(state, 0) + 1
    lines += ["", f"Observation states: {states}.", "",
              "Overlapping signals are not independent trades or a portfolio. The cohorts can contain different stocks; "
              "this comparison does not isolate the causal benefit of news. Fees/slippage are assumptions, not measured fills. "
              "No profitable edge is established. GitHub cache can be evicted; artifacts retain journal snapshots for 90 days."]
    lines += ["", *errors]
    return "\n".join(lines), records

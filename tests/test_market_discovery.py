"""Discovery safety contracts; uses real calendars, indicators and scoring offline."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.services.screening import market_discovery as discovery


NOW = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
TARGET = date(2026, 10, 23)


def history(end=TARGET, falling=False):
    t = np.arange(90)
    close = 100 + (t * (-0.2 if falling else 0.2)) + 2 * np.sin(t)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": 2e6},
                        index=pd.bdate_range(end=end, periods=len(t)))


def quote(symbol="EXMP", **overrides):
    row = {"symbol": symbol, "shortName": "Example company", "quoteType": "EQUITY",
           "currency": "USD", "financialCurrency": "USD", "exchange": "NMS",
           "marketCap": 1e10, "regularMarketPrice": 120,
           "averageDailyVolume3Month": 2e6, "regularMarketChangePercent": 1,
           "regularMarketTime": NOW.timestamp(), "trailingPE": 15, "priceToBook": 2}
    return dict(row, **overrides)


def scan(falling=False):
    frame = discovery.snapshot_rows([quote()], discovery.MARKETS["us"], TARGET)
    data = pd.concat({symbol: history(falling=falling)
                      for symbol in ("EXMP", "^GSPC", "^IXIC")}, axis=1)
    with patch.object(discovery, "fetch_universe", return_value=(frame, 1, 1)), \
            patch.object(discovery.yf, "download", return_value=data):
        return discovery.scan_market("us", NOW, 200, 30, 3)


class DiscoveryTests(unittest.TestCase):
    def test_separate_session_dates_and_phases(self):
        self.assertEqual(discovery.completed_session(discovery.MARKETS["us"], NOW), (TARGET, "premarket"))
        self.assertEqual(discovery.completed_session(discovery.MARKETS["sg"], NOW), (date(2026, 10, 26), "postmarket"))

    def test_weekend_and_close_publication_buffer(self):
        sunday = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)
        self.assertEqual(discovery.completed_session(discovery.MARKETS["us"], sunday), (TARGET, "non-trading day"))
        just_closed = datetime(2026, 10, 26, 20, 5, tzinfo=timezone.utc)
        self.assertEqual(discovery.completed_session(discovery.MARKETS["us"], just_closed), (TARGET, "postmarket"))
        available = datetime(2026, 10, 26, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(discovery.completed_session(discovery.MARKETS["us"], available)[0], date(2026, 10, 26))

    def test_us_holiday(self):
        holiday = datetime(2026, 7, 3, 22, tzinfo=timezone.utc)
        self.assertEqual(discovery.completed_session(discovery.MARKETS["us"], holiday), (date(2026, 7, 2), "non-trading day"))

    def test_currency_receipts_exchange_staleness_and_symbol_validation(self):
        quotes = [quote(), quote("BAD", currency="SGD"), quote("BAD", exchange="PNK"),
                  quote("BAD", shortName="Example ADR"), quote("BAD", regularMarketTime=1),
                  quote("BAD\nstock_list=AAPL"), quote("LOW", regularMarketPrice=1)]
        self.assertEqual(discovery.snapshot_rows(quotes, discovery.MARKETS["us"], TARGET)["code"].tolist(), ["EXMP"])
        sg = quote("D05.SI", currency="SGD", financialCurrency="SGD", exchange="SES")
        sdr = dict(sg, symbol="HBND.SI", shortName="Bank SDR 1to1")
        self.assertEqual(discovery.snapshot_rows([sg, sdr], discovery.MARKETS["sg"], TARGET)["code"].tolist(), ["D05.SI"])

    def test_foreign_currency_valuation_is_missing(self):
        row = discovery.snapshot_rows([quote(financialCurrency="CNY")], discovery.MARKETS["us"], TARGET).iloc[0]
        self.assertIsNone(row["pe_ratio"])
        self.assertIsNone(row["pb_ratio"])

    def test_partial_bar_excluded_and_stale_history_rejected(self):
        bars = history()
        partial = pd.DataFrame({"Open": [999], "High": [999], "Low": [999], "Close": [999], "Volume": [1]},
                               index=pd.to_datetime(["2026-10-26"]))
        complete = discovery.normalized_history(pd.concat([bars, partial]), "EXMP", TARGET)
        self.assertEqual(complete.iloc[-1]["date"], TARGET)
        self.assertLess(complete.iloc[-1]["close"], 999)
        with self.assertRaisesRegex(ValueError, "stale"):
            discovery.normalized_history(bars.iloc[:-1], "EXMP", TARGET)
        with self.assertRaisesRegex(ValueError, "61"):
            discovery.normalized_history(bars.tail(60), "EXMP", TARGET)

    def test_completed_quote_can_supply_missing_last_bar_only(self):
        market = discovery.MARKETS["sg"]
        target = date(2026, 10, 26)
        q = quote("D05.SI", currency="SGD", regularMarketTime=datetime(2026, 10, 26, 9, 5, tzinfo=timezone.utc).timestamp(),
                  regularMarketPrice=120, regularMarketOpen=119, regularMarketDayHigh=121,
                  regularMarketDayLow=118, regularMarketVolume=2e6)
        bar = discovery.closing_bar(q, market, target)
        self.assertIsNotNone(bar)
        result = discovery.normalized_history(history(), "D05.SI", target, bar)
        self.assertEqual(result.iloc[-1]["date"], target)
        self.assertEqual(result.iloc[-1]["close"], 120)
        self.assertTrue(result.attrs["closing_quote_used"])
        # A newer intraday quote must not fill an earlier missing session.
        self.assertIsNone(discovery.closing_bar(q, market, TARGET))
        early = dict(q, regularMarketTime=datetime(2026, 10, 26, 8, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(discovery.closing_bar(early, market, target))
        malformed = dict(q, regularMarketDayHigh=110)
        self.assertIsNone(discovery.closing_bar(malformed, market, target))
        with self.assertRaisesRegex(ValueError, "61"):
            discovery.normalized_history(history().tail(20), "D05.SI", target, bar)

    def test_real_indicators_scores_and_report(self):
        result = scan()
        self.assertEqual([p["code"] for p in result["picks"]], ["EXMP"])
        self.assertEqual(len(result["benchmarks"]), 2)
        report = discovery.render_report({"generated_at": NOW.isoformat(), "markets": [result]})
        self.assertIn("EXMP", report)
        self.assertIn("2026-10-23", report)
        self.assertIn("not probabilities", report)
        json.dumps(discovery.clean_json(result), allow_nan=False)

    def test_declining_market_does_not_force_picks(self):
        result = scan(falling=True)
        self.assertEqual(result["history_valid"], 1)
        self.assertEqual(result["picks"], [])

    def test_all_candidate_histories_missing_is_an_outage(self):
        frame = discovery.snapshot_rows([quote()], discovery.MARKETS["us"], TARGET)
        data = pd.concat({"^GSPC": history(), "^IXIC": history()}, axis=1)
        with patch.object(discovery, "fetch_universe", return_value=(frame, 1, 1)), \
                patch.object(discovery.yf, "download", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "screening unavailable"):
                discovery.scan_market("us", NOW, 200, 30, 3)

    def test_market_failure_keeps_other_market(self):
        def scanner(key, *args):
            if key == "us":
                raise RuntimeError("source unavailable")
            return {"market": key, "status": "ok", "picks": []}
        result = discovery.run(["us", "sg"], NOW, scanner=scanner)
        self.assertEqual([m["status"] for m in result["markets"]], ["unavailable", "ok"])
        self.assertEqual(result["markets"][0]["picks"], [])

    def test_failed_cli_writes_diagnostics_and_empty_output(self):
        def fail(*args):
            raise RuntimeError("network unavailable")
        result = discovery.run(["us", "sg"], NOW, scanner=fail)
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(discovery, "run", return_value=result), \
                        patch.dict(os.environ, {"GITHUB_OUTPUT": str(Path(directory) / "output"),
                                                "STOCK_LIST": "AAPL,MSFT,NVDA"}):
                    self.assertEqual(discovery.main(), 1)
                self.assertEqual(Path("output").read_text(), "stock_list=\n")
                self.assertIn("Unavailable", Path("reports/market_discovery.md").read_text())
                self.assertEqual(len(json.loads(Path("reports/market_discovery.json").read_text())["markets"]), 2)
            finally:
                os.chdir(previous)

    def test_cli_routes_only_discovered_us_symbols_to_ai(self):
        us = scan()
        sg = dict(us, market="sg", picks=[dict(us["picks"][0], code="D05.SI")])
        result = discovery.clean_json({"generated_at": NOW.isoformat(), "markets": [us, sg]})
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(discovery, "run", return_value=result), \
                        patch.dict(os.environ, {"GITHUB_OUTPUT": str(Path(directory) / "output"),
                                                "STOCK_LIST": "AAPL,MSFT,NVDA"}):
                    self.assertEqual(discovery.main(), 0)
                    self.assertEqual(Path("output").read_text(), "stock_list=EXMP\n")
                    Path("output").unlink()
                    result["markets"] = [sg]
                    self.assertEqual(discovery.main(), 0)
                    self.assertEqual(Path("output").read_text(), "stock_list=\n")
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import exchange_calendars as xcals
import pandas as pd

from src.services.screening.news_tracking import observe, update_tracking


def dt(day, hour=22):
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.calendar = xcals.get_calendar("XNYS")
        self.record = {"recorded_at": dt(23).isoformat(), "cost_bps": 20}
        self.bars = pd.DataFrame({"Open": [100, 104, 106], "Close": [103, 105, 104]}, index=pd.to_datetime(["2026-10-26", "2026-10-27", "2026-10-28"]))

    def test_next_future_open_and_costs(self):
        result = observe(self.record, self.bars, self.calendar, dt(26))
        self.assertEqual(result["entry_date"], "2026-10-26")
        self.assertEqual(result["entry_price"], 100)
        self.assertAlmostEqual(result["outcomes"]["1"]["net_pct"], 2.8)
        self.assertNotIn("5", result["outcomes"])

    def test_partial_session_remains_pending(self):
        self.assertEqual(observe(self.record, self.bars, self.calendar, dt(26, 15))["status"], "pending")

    def test_missing_entry_or_intermediate_is_not_shifted(self):
        for bars in (self.bars.iloc[1:], self.bars.iloc[[0, 2]]):
            self.assertEqual(observe(self.record, bars, self.calendar, dt(28))["status"], "unavailable")

    def test_mid_session_signal_uses_next_session_open(self):
        self.record["recorded_at"] = dt(26, 15).isoformat()
        self.assertEqual(observe(self.record, self.bars, self.calendar, dt(28))["entry_date"], "2026-10-27")

    def test_missing_completed_entry_is_unavailable(self):
        self.assertEqual(observe(self.record, self.bars.iloc[:0], self.calendar, dt(26))["status"], "unavailable")

    def test_daily_rerun_preserves_first_signal(self):
        result = {"markets": [{"market": "us", "status": "ok", "picks": [{"code": "AAPL", "screen_score": 71}], "news_rankings": []}]}
        with tempfile.TemporaryDirectory() as directory:
            _, original = update_tracking(result, dt(23), directory, download=Mock(return_value=self.bars))
            result["markets"][0]["picks"][0]["screen_score"] = 99
            _, updated = update_tracking(result, dt(23, 23), directory, download=Mock(return_value=self.bars))
        self.assertEqual(len(updated), 1)
        self.assertEqual(original[0]["recorded_at"], updated[0]["recorded_at"])
        self.assertEqual(updated[0]["score"], 71)


if __name__ == "__main__":
    unittest.main()

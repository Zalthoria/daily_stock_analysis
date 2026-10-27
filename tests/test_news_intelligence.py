"""Evidence, freshness, ambiguity and price-timing regression checks."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from src.services.screening import market_discovery as discovery
from src.services.screening import news_intelligence as news
from src.services.empty_news import guard_news_checklist
from tests.test_market_discovery import history, quote, NOW as SCAN_NOW, TARGET

NOW = datetime(2026, 10, 26, 22, tzinfo=timezone.utc)
ROW = {"code": "AAPL", "name": "Apple Inc.", "price": 110, "ma20": 105, "bar_date": "2026-10-26",
       "amount": 2e8, "volume_ratio_20d": 1.5, "screen_score": 80}


def story(title="Apple raises earnings forecast", when=None, url="https://example.com/a"):
    return news.article(title, "Apple raises earnings forecast above consensus estimates.", url,
                        (when or NOW - timedelta(hours=4)).isoformat(), "Example", NOW, "us")


def event(item, direction="positive", impact=2, surprise=1):
    return news.validate_event({"direction": direction, "impact": impact, "surprise": surprise,
                               "evidence": item["title"], "surprise_evidence": item["excerpt"],
                               "reason": "Explicit guidance change"}, item)


def scored(stories, events, close=None):
    return news.score_candidate(ROW, stories, events, discovery.MARKETS["us"], NOW,
                                close or NOW - timedelta(hours=2), True, True)


class NewsTests(unittest.TestCase):
    def test_unknown_stale_future_and_unsafe_sources_rejected(self):
        for date in (None, "bad", "2026-10-26", (NOW - timedelta(days=5)).isoformat(), (NOW + timedelta(seconds=1)).isoformat()):
            self.assertIsNone(news.article("Title", "", "https://example.com", date, "Example", NOW, "us"))
        self.assertIsNone(news.safe_url("javascript:alert(1)"))
        self.assertIsNone(news.safe_url("https://name:password@example.com"))
        self.assertNotIn(")", news.safe_url("https://example.com/a)"))

    def test_duplicate_headlines_do_not_add_scores(self):
        first, second = story(), story(url="https://example.org/reprint")
        result = news.deduplicate([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["duplicate_count"], 2)
        self.assertEqual(news.deduplicate(result)[0]["duplicate_count"], 2)

    def test_company_alias_and_ambiguous_ticker(self):
        item = story("DBS announces acquisition")
        self.assertTrue(news.matches({"code": "D05.SI", "name": "DBS Group Holdings"}, [item]))
        self.assertFalse(news.matches({"code": "ON", "name": "ON Semiconductor Corporation"}, [story("Policy on new exports")] ))

    def test_similar_headlines_with_reversed_meaning_stay_separate(self):
        first = story("Apple wins major patent lawsuit in US federal court")
        second = story("Apple loses major patent lawsuit in US federal court", url="https://example.com/reversal")
        self.assertEqual(len(news.deduplicate([first, second])), 2)

    def test_multi_company_article_gets_separate_target_ids(self):
        item = story("Apple wins contract from Google")
        first = news.company_stories(ROW, [item])[0]
        second = news.company_stories({"code": "GOOG", "name": "Alphabet Inc."}, [item])[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["source_article_id"], second["source_article_id"])

    def test_fabricated_quote_rejected_and_unknown_surprise_zero(self):
        item = story()
        with self.assertRaises(ValueError):
            news.validate_event({"direction": "positive", "impact": 3, "evidence": "Not present"}, item)
        value = event(item)
        value["surprise_evidence"] = "Missing comparison"
        self.assertEqual(news.validate_event(value, item)["surprise"], 0)
        self.assertEqual(event(item, "unknown", 3, 2)["impact"], 0)

    def test_model_failure_remains_unscored(self):
        item = news.company_stories(ROW, [story()])[0]
        events, errors = news.extract_events([item], generate=Mock(side_effect=RuntimeError("unavailable")))
        result = scored([item], events)
        self.assertTrue(errors)
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "unscored")
        self.assertEqual(result["components"], {})

    def test_only_requested_valid_model_ids_accepted(self):
        item = news.company_stories(ROW, [story()])[0]
        valid = dict(event(item), id=item["id"])
        generated = Mock(return_value=json.dumps({"events": [dict(valid, id="invented"), valid]}))
        events, errors = news.extract_events([item], generated)
        self.assertFalse(errors)
        self.assertEqual(set(events), {item["id"]})
        self.assertEqual(events[item["id"]]["status"], "extracted")

    def test_timeout_retried_but_forbidden_not_retried(self):
        with patch.object(news.requests, "get", side_effect=requests.Timeout("timeout")) as get:
            rows, status = news.fetch_feed(news.FEEDS[0], NOW)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(status["status"], "failed")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
        with patch.object(news.requests, "get", return_value=response) as get:
            news.fetch_feed(news.FEEDS[0], NOW)
        self.assertEqual(get.call_count, 1)

    def test_news_after_price_cannot_be_confirmed(self):
        item = story(when=NOW - timedelta(hours=1))
        result = scored([item], {item["id"]: event(item)})
        self.assertEqual(result["status"], "watch")
        self.assertEqual(result["components"]["price_volume"], 0)

    def test_adverse_material_event_blocks_positive_candidate(self):
        good = story()
        bad = story("Apple loses major lawsuit", url="https://example.com/b")
        result = scored([good, bad], {good["id"]: event(good), bad["id"]: event(bad, "negative")})
        self.assertEqual(result["status"], "risk_alert")
        self.assertEqual(scored([good], {good["id"]: event(good)})["status"], "candidate")

    def test_no_evidence_never_means_no_adverse_news(self):
        result = scored([], {})
        self.assertIsNone(result["score"])
        dashboard = {"battle_plan": {"action_checklist": ["✅ no major negative news", "Trend checked"]}}
        guard_news_checklist(dashboard)
        self.assertIn("unverified", dashboard["battle_plan"]["action_checklist"][0])
        self.assertEqual(dashboard["battle_plan"]["action_checklist"][1], "Trend checked")

    def test_context_requires_fresh_actual_articles(self):
        item = story()
        row = scored([item], {item["id"]: event(item)})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            data = {"generated_at": NOW.isoformat(), "markets": [{"news_rankings": [row]}]}
            path.write_text(json.dumps(data))
            self.assertEqual(news.load_stock_context("AAPL", path, NOW)[1], 1)
            self.assertEqual(news.load_stock_context("AAPL", path, NOW + timedelta(hours=13))[1], 0)
            row["events"] = []
            path.write_text(json.dumps(data))
            self.assertEqual(news.load_stock_context("AAPL", path, NOW)[1], 0)

    def test_news_expands_pool_without_changing_baseline_picks(self):
        frame = discovery.snapshot_rows([quote("EXMP", shortName="Example Inc."), quote("NEWS", shortName="Newsworthy Inc.", regularMarketChangePercent=-2)], discovery.MARKETS["us"], TARGET)
        data = pd.concat({symbol: history() for symbol in ("EXMP", "NEWS", "^GSPC", "^IXIC")}, axis=1)
        item = news.article("Newsworthy announces acquisition", "", "https://example.com/news", SCAN_NOW.isoformat(), "Example", SCAN_NOW, "us")
        bundle = {"articles": [item], "sources": []}
        with patch.object(discovery, "fetch_universe", return_value=(frame, 2, 2)), patch.object(discovery.yf, "download", return_value=data), patch.object(news, "company_news"), patch.object(news, "extract_events", return_value=({}, [])):
            baseline = discovery.scan_market("us", SCAN_NOW, 200, 1, 1)
            enriched = discovery.scan_market("us", SCAN_NOW, 200, 1, 1, bundle)
        self.assertEqual(baseline["picks"], enriched["picks"])
        self.assertEqual(enriched["history_attempted"], 2)
        self.assertEqual(enriched.get("news_errors"), [])


if __name__ == "__main__":
    unittest.main()

# Automatic US and Singapore market discovery

This fork's daily Actions workflow discovers research candidates without a personal watchlist. It runs Monday–Friday at 10:00 UTC (18:00 Singapore), subject to GitHub scheduling delays. US data normally comes from the previous completed session; Singapore data normally comes from that day's completed session. Holidays use the most recent completed exchange session. A 20-minute publication buffer prevents a newly closing session from being treated as complete too early.

Open the latest **每日股票分析** run in Actions. Its summary contains the market overview and shortlist. The `analysis-reports-N` artifact includes `reports/market_discovery.md`, an auditable `reports/market_discovery.json`, and the existing AI report when US candidates are selected. Artifacts are retained for 90 days. The [news research layer](news-research.md) adds `news_research.md`, `news_sources.json` and `news_signal_history.json`. No email or chat delivery is configured by this feature.

## Coverage and selection

The Yahoo Finance screener supplies a fresh universe on every run. There is no static sample list. By default, fetch the largest 200 listings meeting the screener's price, capitalization and volume filters in each market. Validate exchange, listing currency and quote freshness; exclude identifiable depositary receipts, warrants, leveraged and inverse products. This is bounded liquid-stock coverage, not exhaustive exchange coverage. REITs can qualify.

| Market | Exchanges | Minimum price | Minimum market cap | Average shares/day (3 months) | Average traded value/day |
|---|---|---:|---:|---:|---:|
| US | NYSE, Nasdaq, NYSE American | USD 5 | USD 2 billion | 300,000 | USD 10 million |
| Singapore | SGX / Yahoo SES, SGD listings | SGD 0.20 | SGD 100 million | 20,000 | SGD 500,000 |

The quote prefilter estimates traded value using price × average volume. Final eligibility uses average adjusted close × volume over 20 completed sessions. Snapshot ranking uses DSA's existing scorer: 40% momentum, 40% liquidity and 20% value. Only the best 30 snapshots per market receive six months of history checks. The final score uses 50% momentum, 20% stability, 20% liquidity and 10% value. PE/PB are treated as missing if financial and listing currencies differ. Missing valuation receives the existing scorer's conservative treatment.

Final candidates must have at least 61 complete daily bars ending on the expected exchange session, positive prices, nonnegative volume, price > MA20 >= MA60, positive 60-session return, RSI(14) <= 75, price <= 110% of MA20, daily data quality >= 80, and the final price/traded-value minimums. Scores are heuristic rankings within a market, not success probabilities or backtested returns. Up to three candidates per market are selected. Zero qualifying candidates is a valid result.

The overview calculates S&P 500, Nasdaq Composite and Straits Times Index returns and trend position from completed-session data. It states the session date and current exchange phase separately. Prices are adjusted by yfinance; intraday snapshot changes influence only the preliminary pool. The main report is calculated, without an LLM-generated market recap.

Yahoo sometimes leaves the latest daily Close/Adj Close empty after an exchange closes. Only that latest completed bar may be supplied from its timestamped regular-session OHLC quote, with the same currency, the exact expected session date, a timestamp within 15 minutes of session close or later, and internally valid prices/volume. The exchange's 20-minute closing buffer still applies. The report and JSON explicitly identify quote-derived bars. Older gaps are never synthesized and quotes from another session are never used. Index closing quotes use the same checks; indices may have zero reported volume.

Selected **US** symbols are passed to the existing Gemini analysis pipeline. **Singapore** receives the quantitative overview and shortlist only: this does not add Singapore support to DSA's single-stock routing, API or UI. News, earnings dates and catalysts are not verified by this screen. The existing US report can use its separately configured search providers; adding this feature does not configure one.

## Configuration

Set these under Settings → Secrets and variables → Actions → Variables:

| Variable | Default | Purpose |
|---|---|---|
| `AUTO_DISCOVERY_ENABLED` | `true` | Daily workflow only. Set `false` to restore original manual `STOCK_LIST` mode. |
| `DISCOVERY_MARKETS` | `us,sg` | `us`, `sg`, or both. |
| `DISCOVERY_POOL_SIZE` | `200` | Largest matching listings fetched per market, at most 250. |
| `DISCOVERY_HISTORY_LIMIT` | `30` | Snapshot leaders whose history is checked per market. |
| `DISCOVERY_TOP_N` | `3` | Final candidates per market, 1–5. |

Require `top <= history <= pool <= 250`. Discovery needs no API key. US AI analysis retains the existing model credentials and configuration. `STOCK_LIST` and the legacy market recap are ignored while discovery is enabled. No candidates means no US AI step, with no sample substitution. Manual `market-only` publishes the discovery report and skips the AI step; `full` and `stocks-only` both discover first and analyze selected US candidates.

For local execution, install `requirements.txt`, export the desired `DISCOVERY_*` variables, and run:

```bash
python -m src.services.screening.market_discovery
python -m unittest tests.test_market_discovery
```

The standalone discovery CLI reads process environment, not `.env`, and never invokes an LLM. Other entry points (`main.py`, Docker, API, Web and desktop) retain their existing behavior.

## Failure behavior and rollback

Stale or incomplete individual histories are excluded and recorded in JSON. A market outage is reported explicitly, and the other market can still produce a report. If both markets fail, the CLI and workflow fail while preserving diagnostic reports. If every attempted candidate history fails, the market is unavailable rather than falsely reporting that no stocks qualified. Data can still contain vendor errors; this is one-source research screening, not validated investment advice or trading automation.

Set `AUTO_DISCOVERY_ENABLED=false` to restore the previous watchlist workflow, or revert the feature commit. Existing model secrets and saved watchlists are preserved. This feature document is English-only because this fork's reports are English; no existing bilingual document pair was changed.

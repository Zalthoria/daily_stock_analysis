# Daily news research and forward tracking

The existing weekday 18:00 Singapore workflow now combines automatic US/SG stock discovery with bounded public-news collection. No personal watchlist is required. It produces research priorities, with reasons and links, rather than placing orders.

## Collection and coverage

The collector attempts CNA Business/Singapore, CNBC, MarketWatch, Federal Reserve releases and SEC's XBRL filing feed. It also attempts Yahoo company news for up to three shortlisted companies per market. Each source reports success, no recent items or failure. SEC metadata is not a reading of the underlying filing. SGX structured announcements and hosted SearXNG are explicitly unconfigured; no paid subscription or hosted search instance is created.

Only timestamped items within 72 hours are accepted by default. Future/undated items and unsafe URLs are rejected. HTML is removed, excerpts are capped at 800 characters, and duplicate URLs/headlines are combined. Links and attribution are retained. These public feeds have incomplete coverage and may fail or change. Publisher content is not open-source merely because an RSS feed is public; respect source terms, including CNA's personal/non-commercial limits. Full articles are not republished.

Companies are matched using full names, conservative name variants and explicit aliases, including common Singapore names. Ambiguous bare ticker words do not trigger matches. Within the existing capped universe, up to ten extra news-matched companies per market get price-history checks. Original price-only picks and scores remain independently calculated, so the comparison baseline is preserved.

For each company, at most four recent articles are retained. Up to 24 company/article pairs per market are submitted in batches of eight to the configured model. A story affecting multiple companies is evaluated separately for each. Only an exact evidence quote is accepted. The model may still misinterpret a real quote; read the originals. Unknown extraction yields an unscored result, never an invented catalyst.

## Explainable score

| Component | Maximum | Evidence |
|---|---:|---|
| Company impact | 30 | Excerpt-backed materiality assessment |
| Surprise | 20 | Explicit comparison with expectations; growth alone is insufficient |
| Price/volume | 20 | Eligible trend and relative volume >=1.2, only after news publication |
| Freshness | 10 | Declines from 10 to 2 over the configured window |
| Market trend | 10 | Available benchmark closes above MA20 |
| Liquidity | 10 | Market-specific traded-value threshold |

The highest individual positive-event score is used. Duplicate stories do not accumulate points. Material negative/mixed evidence overrides candidate status with a risk alert. Candidate status additionally requires a material positive catalyst, score >=60, the existing trend/stretch rules, and a completed price session after publication. News published after the last price bar stays on watch even if other points are high. Reports show the price move since the last completed close before publication when measurable.

These are heuristic priority points, not calibrated probabilities, expected returns or a validated trading strategy. Confidence describes evidence quality, separately from direction and score. Sector relative strength, spreads, full filings and independent analyst consensus are unavailable. No evidence does not certify absence of adverse news. High scores can describe already-priced-in events.

## Reports and AI context

The Actions summary contains `market_discovery.md` and `news_research.md`. The `analysis-reports-N` artifact retains those plus `market_discovery.json`, `news_sources.json` and `news_signal_history.json` for 90 days. Reports separate candidate, watch, risk alert, unscored and no-evidence outcomes. Up to three news-confirmed US candidates followed by original US picks (five unique symbols maximum) reach the existing AI pipeline. Singapore candidates remain in the quantitative/news report, outside DSA's unsupported Singapore single-stock routing.

Actual dated discovery articles are passed to both existing analysis paths through their shared context loader. The report must be at most 12 hours old. Empty placeholders never count as supplied evidence. Checklist claims that no negative news exists are changed to an incomplete-coverage warning.

## Prospective evaluation

The journal records the first daily price-only and news-confirmed signals after scoring completes. Repeated runs do not rewrite that day's first observation. Simulated entry is the first exchange open strictly after the record timestamp, with 1/5/20 completed-session closing returns and the worst completed close versus entry. Adjusted prices are used, with an illustrative 20-basis-point round-trip cost. No actual orders are sent. A missing entry or intermediate session makes the result unavailable instead of moving the entry date. Partial sessions do not count.

Overlapping signals are not independent observations or a portfolio. Cohorts can contain different stocks; their comparison does not isolate news' causal benefit. Costs are assumptions, and delistings, corporate actions or missing data can bias results. Records beyond the bounded history window are marked unavailable. A useful profitability conclusion requires enough prospective observations and more rigorous evaluation.

GitHub cache restores `data/news_state/signals.json`; a new run saves a unique snapshot. Cache can be evicted. Missing/corrupt journals explicitly start a new tracking period; corrupt files are preserved. Download the artifact journals for longer retention. This is not a permanent performance database.

## Configuration and rollback

CLI news collection is opt-in with `DISCOVERY_NEWS_ENABLED=true`; this fork's Actions default is true. Existing configured model secrets are reused. Optional variables: `DISCOVERY_NEWS_MAX_AGE_HOURS` (1–168, default 72), `NEWS_ROUND_TRIP_COST_BPS` (0–1000, default 20). `NEWS_USER_AGENT` identifies feed requests. Local overrides include `NEWS_STATE_DIR` and `DISCOVERY_NEWS_CONTEXT_PATH`; `.env.example` lists them.

Set repository variable `DISCOVERY_NEWS_ENABLED=false` to restore price-only automatic discovery. `AUTO_DISCOVERY_ENABLED=false` restores the original manual mode. The schedule and existing notification destinations are unchanged. This fork-specific guide is English-only; upstream bilingual guides are not changed.

Validation covers timestamp and URL rejection, duplicates, ambiguous tickers, target-specific extraction, quote validation, source/model failure, publication-vs-price timing, adverse overrides, fresh context, unchanged baseline picks and next-open journal accounting. Local tests do not establish a profitable edge or guarantee remote data/model availability. The live Actions report is the operational check.

# Pre-Market Scoring Engine — Planning Prompt

A prompt to hand to Codex so it can produce an architecture plan for TRD's pre-market scoring
engine. **Kept verbatim**, same convention as `CODE_REVIEW.md` and
`FEATURE_REQUEST_EARNINGS_FOLLOWTHROUGH.md`: the document is the input, and the judgement about
what survives contact with the codebase lives in SB-789 rather than in edits here.

Notably, roughly half of what it asks for exists already — the `MarketDataProvider` protocol, the
score-plus-reason signal contract, the `engine_signal` decision log, and `SundayPrepService`'s
weekly assembly of the same inputs. The ticket says which. Read them together.

---

You are a Staff Plus Software Architect and Quant Systems Engineer.

Your task is NOT to write code, but to design a production-ready implementation plan for TRD's
pre-market scoring engine.

Goal: produce a ranked watchlist each morning that answers "Why this stock today?"

Morning flow:

- 8:00 — load universe.
- 8:05 — pull pre-market quotes, news, earnings, upgrades, sector data.
- 8:20 — calculate score.
- 9:20 — finalize rank.
- 9:30+ — wait for entry confirmation.

Scoring should be modular with weights and explanations.

Build provider interfaces so data sources can be swapped. Start with Yahoo Finance via yfinance,
FMP, Finnhub; plan for Polygon later.

Include logging of every decision for observability, analysis, and backtesting.

Recommend project structure, data schema, scoring formula design, risk controls, and analytics
dashboards.

Output a phased task list suitable for coding agents.

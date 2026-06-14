---
name: week-ahead
description: Review the trd Sunday Prep briefing and turn it into personalized, mentor-style insight for the week ahead. Connects the market setup (futures, VIX, FOMC/macro calendar, sector leadership, SPY/QQQ/IWM levels, themes) to the user's OWN holdings, watchlist, and simulation/DCA plans. Use when the user runs /week-ahead, asks "what should I watch this week", wants the Sunday prep reviewed/analyzed/explained, or wants weekly market insight tied to their portfolio.
allowed-tools: Bash, Read
---

# Week Ahead — review the Sunday Prep and add insight

`trd sunday-prep` produces the *data*: futures, the macro calendar, earnings, sector
leadership, the VIX read, key levels, themes, a watchlist, and risks. Your job is the
*insight* — read that briefing, cross-reference the user's actual book, and deliver a
calm, experienced-mentor read of the week. Synthesis, not restatement.

This is educational market commentary, **not financial advice**. Never tell the user to
buy or sell. Frame everything as "here's the setup, here's what to watch, here's a plan."

## Step 0 — Environment

trd's data lives in the iCloud DB. A non-interactive shell may not have `TRD_HOME` set,
so prefix trd calls (or export once at the start of your Bash block):

```bash
export TRD_HOME="${TRD_HOME:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd}"
```

If any `trd` command says "command not found", it's at `~/.local/bin/trd` — add that to
PATH or call it directly.

## Step 1 — Be smart about freshness (time + whether prep has run)

Sunday Prep is a Sunday-evening ritual (the scheduled job runs Sundays ~6:05 PM ET). Work
out what data to use:

```bash
date "+%A %Y-%m-%d %I:%M %p %Z"      # today, weekday, local time
trd sunday-prep --history 3           # latest saved snapshots (date, VIX, leadership)
ls -t "$TRD_HOME/prep/"*.md 2>/dev/null | head -3   # snapshot files, newest first
```

Decide:
- **A fresh snapshot exists for the current week** (newest snapshot dated on/after the most
  recent Sunday) → read it (`Read` the newest `prep/<date>.md`). Fast, offline, and it's the
  exact briefing the user will have seen. Say which snapshot you're using.
- **No snapshot this week** → run it live: `trd sunday-prep --json` (≈1-2 min, hits yfinance).
  Parse the JSON. Mention you generated it live because no snapshot was found.
- **It's Sunday before ~6 PM ET** → the week's prep may not have run yet and futures/levels
  will still move after the futures open; note that the read is provisional and worth
  refreshing tonight.
- **It's mid-week** → the briefing is still the right forward read for the *current* week;
  just acknowledge we're partway through it.

Prefer the JSON when running live (easier to reason over precisely); the `.md` snapshot is
fine to read directly.

## Step 2 — Pull the user's context (personalize)

The whole point is tying the market setup to *this* user. Gather:

```bash
trd portfolio              # real holdings, weights, P&L
trd watch ls               # watchlist board (things they're tracking)
trd dca ls                 # contribution/sim plans (paper accounts + DCA)
trd earnings --days 8      # earnings across everything they track, next week
trd equity --months 6      # recent portfolio trajectory (drawdown/return context)
```

From these, build the cross-reference:
- Which **holdings** report earnings this week, or sit in the week's leading/lagging sectors.
- Which holdings are **near the key levels** the briefing flagged (SPY/QQQ/IWM), or are
  rate-sensitive into an FOMC week.
- **Watchlist / sim** names with a catalyst this week (earnings, a themed move).
- The portfolio's **tilt** vs where leadership is (concentrated in a lagging sector? riding a
  leader?) and its recent drawdown/return from the equity curve.

## Step 3 — Deliver the insight

Write a tight briefing (not a data dump — the user already has the tables). Structure:

- **This week in one line.** The single dominant driver (e.g. "It's an FOMC week; everything
  keys off Wednesday 2 PM").
- **The setup.** 2-4 sentences synthesizing futures + VIX regime + leadership into a coherent
  read. Cite the actual numbers from the briefing.
- **For your book.** The specific, personalized callouts: your holdings exposed to this week's
  catalysts (name them, with the date/level), your portfolio's tilt vs leadership, where the
  equity curve sits.
- **On your radar.** Watchlist / sim names with a concrete catalyst this week.
- **Risk to you.** The biggest risk to *this* portfolio specifically — sized, not generic.
- **The plan.** If-right / if-wrong framing and what would change the thesis. Disciplined and
  reactive, never a prediction or a trade call.
- Close with: **"What's the one thing you're watching closely this week?"**

## Tone & guardrails

- Experienced, calm mentor. Practical and educational. No hype, no certainty, no price targets.
- Not financial advice; no buy/sell directives. "Watch", "if X then consider", "the risk is".
- Ground every claim in the briefing's data or the user's actual positions — cite specifics.
- Be concise. A good week-ahead is a page, not an essay.

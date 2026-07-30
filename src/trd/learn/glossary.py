"""The learn-to-invest dictionary: every term trd shows and every formula trd
computes, with worked examples. Numbers on screen -> `trd learn <term>` -> the
exact formula used.

Indicator, entry-strategy and exit-rule entries are generated from their code
registries, so a rule's description lives with the rule and the dictionary
cannot drift from what actually runs. Everything else is written by hand,
because a concept like lookahead bias has no registry to read it from."""

from enum import StrEnum

from pydantic import BaseModel

from trd.engine import EXIT_RULES
from trd.engine import REGISTRY as STRATEGY_REGISTRY
from trd.indicators import REGISTRY as INDICATOR_REGISTRY


class Category(StrEnum):
    BASICS = "basics"
    RETURNS = "returns"
    DCA = "dca"
    INDICATORS = "indicators"
    ENGINE = "engine"
    ACCOUNTS_TAX = "accounts & tax"


class GlossaryEntry(BaseModel):
    key: str
    term: str
    category: Category
    definition: str
    formula: str | None = None
    example: str | None = None
    related: list[str] = []
    used_in: list[str] = []  # trd commands where this number appears


_ENTRIES: list[GlossaryEntry] = [
    # ── basics ────────────────────────────────────────────────────────────
    GlossaryEntry(
        key="pl",
        term="P&L (profit and loss)",
        category=Category.BASICS,
        definition=(
            "What you've made or lost on a position: current value minus what you paid. "
            "Unrealized until you sell."
        ),
        formula="P&L = market value - cost basis\nP&L% = P&L / cost basis x 100",
        example="Paid $2,000 for QQQ, now worth $2,600 -> P&L = +$600 (+30%).",
        related=["cost-basis", "unrealized", "xirr"],
        used_in=["trd portfolio", "trd lots", "trd dca show"],
    ),
    GlossaryEntry(
        key="unrealized",
        term="Unrealized vs realized",
        category=Category.BASICS,
        definition=(
            "Unrealized gains are paper gains — you still hold the shares and the price can "
            "change. Realized gains lock in when you sell, and only realized gains are taxed."
        ),
        example=(
            "Your ACME position is down $4,000 unrealized: a real loss only if you sell at this price."
        ),
        related=["pl", "cost-basis"],
        used_in=["trd portfolio"],
    ),
    GlossaryEntry(
        key="cost-basis",
        term="Cost basis",
        category=Category.BASICS,
        definition=(
            "Total amount paid for what you currently hold, including fees. The anchor "
            "every gain/loss is measured against — and what the IRS cares about."
        ),
        formula="cost basis = sum over open lots of (quantity x price paid + fees)",
        example="Buy 10 @ $100 + $5 fee = $1,005 basis. Sell 5 (FIFO) -> remaining basis $502.50.",
        related=["fifo", "avg-cost", "pl"],
        used_in=["trd portfolio", "trd lots"],
    ),
    GlossaryEntry(
        key="fifo",
        term="FIFO lots",
        category=Category.BASICS,
        definition=(
            "First In, First Out: when you sell, your oldest shares are sold first. trd "
            "derives every holding from its transaction history this way — per account, "
            "so a sell at one broker never touches another broker's lots."
        ),
        formula=(
            "sell consumes lots oldest-first; a partially sold lot keeps a "
            "proportional share of its cost"
        ),
        example=(
            "Lots: 10 @ $100 (2020), 10 @ $200 (2024). Sell 15 -> 5 left from "
            "the 2024 lot, basis $1,000."
        ),
        related=["cost-basis", "avg-cost"],
        used_in=["trd lots", "trd portfolio"],
    ),
    GlossaryEntry(
        key="avg-cost",
        term="Average cost",
        category=Category.BASICS,
        definition="Cost basis divided by shares held — the per-share break-even before fees.",
        formula="avg cost = cost basis / quantity held",
        example="$1,420 basis / 40 QQQ = $35.50 per share.",
        related=["cost-basis", "fifo"],
        used_in=["trd portfolio", "trd lots", "trd dca show"],
    ),
    GlossaryEntry(
        key="day-change",
        term="Day change",
        category=Category.BASICS,
        definition="Today's move: current price vs yesterday's close, in dollars and percent.",
        formula=(
            "day change = (price - previous close) x quantity\nday change % = (price - "
            "previous close) / previous close x 100"
        ),
        related=["pl"],
        used_in=["trd portfolio", "trd watch ls"],
    ),
    GlossaryEntry(
        key="dividend",
        term="Dividend",
        category=Category.BASICS,
        definition=(
            "Cash a company pays per share, usually quarterly. SPY/QQQ yield roughly "
            "1-1.5%/year — invisible in price-only charts but real money."
        ),
        related=["drip", "adjusted-close"],
        used_in=["trd dca forecast (via adjusted closes)"],
    ),
    GlossaryEntry(
        key="drip",
        term="DRIP (dividend reinvestment)",
        category=Category.BASICS,
        definition=(
            "Automatically using each dividend to buy more shares (often fractions). Each "
            "reinvestment is a new lot — the tiny quarterly lots in your Fidelity history."
        ),
        example="A holding pays a $15 dividend -> broker buys 0.217 more shares that day -> new lot.",
        related=["dividend", "fifo"],
        used_in=["trd lots"],
    ),
    GlossaryEntry(
        key="paper-trading",
        term="Paper trading (simulation)",
        category=Category.BASICS,
        definition=(
            "Pretend money, real prices. trd simulation accounts run strategies on paper so "
            "you can compare ideas risk-free before committing real dollars."
        ),
        related=["dca", "benchmark"],
        used_in=["trd sim", "trd dca ls (type column)"],
    ),
    # ── returns ───────────────────────────────────────────────────────────
    GlossaryEntry(
        key="xirr",
        term="XIRR (money-weighted return)",
        category=Category.RETURNS,
        definition=(
            "The single annual rate that makes all your dated cashflows (buys out, value "
            "back) break even. THE honest metric for DCA: simple P&L% punishes recent "
            "contributions that haven't had time to grow; XIRR weights each dollar by how "
            "long it was invested."
        ),
        formula=(
            "solve r so that: sum of cashflow_i / (1+r)^(years_i) = 0\n"
            "buys are negative flows, current value is the final positive flow\n"
            "trd solves by bisection; needs 30+ days of history"
        ),
        example=(
            "$100/month for 12 months ($1,200 in), worth $1,290 at year end -> "
            "simple P&L% = 7.5%, but XIRR ~ 14%/yr — the average dollar was "
            "only invested ~6 months."
        ),
        related=["cagr", "pl"],
        used_in=["trd dca show", "trd dca backtest"],
    ),
    GlossaryEntry(
        key="cagr",
        term="CAGR / geometric mean return",
        category=Category.RETURNS,
        definition=(
            "Compound annual growth rate: the steady yearly rate that produces the same "
            "end result. Built from the geometric mean of period returns — the right way "
            "to average returns (arithmetic mean overstates: +50% then -50% is -25%, not 0%)."
        ),
        formula=("monthly: g = (product of (1+R_t))^(1/T) - 1\nannualized: CAGR = (1+g)^12 - 1"),
        example="Monthly returns +2%, -1%, +3% -> g = (1.02 x 0.99 x 1.03)^(1/3) - 1 = 1.32%/mo.",
        related=["xirr", "monte-carlo"],
        used_in=["trd dca forecast"],
    ),
    GlossaryEntry(
        key="benchmark",
        term="Benchmark (SPY same-dates)",
        category=Category.RETURNS,
        definition=(
            "What your exact contributions would be worth if every dollar had bought plain "
            "SPY on the same days instead. Answers: did my strategy beat doing nothing clever?"
        ),
        formula=(
            "for each contribution: spy shares += amount / SPY close that day\n"
            "benchmark value = total spy shares x SPY price today"
        ),
        related=["xirr", "dca"],
        used_in=["trd dca show", "trd dca status", "trd dca backtest"],
    ),
    GlossaryEntry(
        key="adjusted-close",
        term="Adjusted close",
        category=Category.RETURNS,
        definition=(
            "Historical price corrected for splits and dividends. Multi-year return math "
            "on raw closes silently drops dividends and breaks across splits — trd uses "
            "adjusted closes for all return/forecast math, raw prices for your ledger."
        ),
        example="SMH split 2024: raw chart shows a cliff; adjusted series is continuous.",
        related=["dividend", "cagr"],
        used_in=["trd dca forecast", "trd dca backtest"],
    ),
    GlossaryEntry(
        key="monte-carlo",
        term="Monte Carlo simulation (bootstrap)",
        category=Category.RETURNS,
        definition=(
            "Instead of one prediction, run thousands of randomized futures: each month "
            "draw a random month from your allocation's actual history. The spread of "
            "outcomes shows the uncertainty a single projection hides."
        ),
        formula=(
            "per trial, per month: value = (value + contribution) "
            "x (1 + randomly drawn historical monthly return)\n"
            "repeat 1,000 trials -> read percentiles of the outcomes"
        ),
        related=["percentiles", "cagr"],
        used_in=["trd dca forecast"],
    ),
    GlossaryEntry(
        key="percentiles",
        term="Percentile bands (p10/p50/p90)",
        category=Category.RETURNS,
        definition=(
            "Of 1,000 simulated futures: p10 = 10% ended below this (bad-case), p50 = "
            "median, p90 = 10% ended above (good-case). NOT guarantees — the band only "
            "reflects what your historical window contained."
        ),
        related=["monte-carlo"],
        used_in=["trd dca forecast"],
    ),
    GlossaryEntry(
        key="future-value",
        term="Future value of monthly investing",
        category=Category.RETURNS,
        definition=(
            "Closed-form projection of contributing C every month at steady monthly rate g "
            "(annuity-due: you contribute at the start of each month, then it grows)."
        ),
        formula=(
            "FV = V0 x (1+g)^M + C x ((1+g)^M - 1)/g x (1+g)\nV0 = today's value, M "
            "= months, C = monthly contribution"
        ),
        example="$100/mo for 10y at g=0.8%/mo -> FV ~ $19.4k on $12k contributed.",
        related=["cagr", "monte-carlo"],
        used_in=["trd dca forecast"],
    ),
    # ── dca ───────────────────────────────────────────────────────────────
    GlossaryEntry(
        key="dca",
        term="DCA (dollar-cost averaging)",
        category=Category.DCA,
        definition=(
            "Investing a fixed dollar amount on a fixed schedule regardless of price. You "
            "automatically buy more shares when prices are low, fewer when high — removes "
            "timing decisions and emotion. trd's flagship workflow."
        ),
        formula="shares bought each month = fixed $ amount / that day's price",
        example=(
            "$100 buys 0.139 SPY at $720, but 0.151 SPY at $660 — same "
            "habit, more shares when cheap."
        ),
        related=["xirr", "allocation", "benchmark"],
        used_in=["trd dca (everything)"],
    ),
    GlossaryEntry(
        key="allocation",
        term="Allocation / weights",
        category=Category.DCA,
        definition=(
            "How each contribution splits across holdings, in percent. Your plan: 40% SPY / "
            "40% QQQ / 10% SMH / 10% ARKX of every $100."
        ),
        formula="leg amount = monthly amount x weight / 100",
        related=["drift", "dca"],
        used_in=["trd dca set --alloc", "trd dca show"],
    ),
    GlossaryEntry(
        key="drift",
        term="Weight drift",
        category=Category.DCA,
        definition=(
            "How far a holding's actual share of your plan has wandered from its target "
            "weight, in percentage points. Winners drift overweight. Each fresh contribution "
            "at target weights partially pulls it back."
        ),
        formula=(
            "drift = actual weight - target weight\nactual weight = holding "
            "value / total plan value x 100"
        ),
        example=(
            "QQQ target 40%, now 47% of plan value -> drift +7pp "
            "(overweight — QQQ outran the rest)."
        ),
        related=["allocation", "rebalancing"],
        used_in=["trd dca show"],
    ),
    GlossaryEntry(
        key="rebalancing",
        term="Rebalancing",
        category=Category.DCA,
        definition=(
            "Restoring drifted weights back to target — selling overweight winners and/or "
            "buying underweight laggards. DCA plans partially self-rebalance because every "
            "contribution lands at target weights."
        ),
        related=["drift", "allocation"],
        used_in=["trd dca show (drift column tells you when)"],
    ),
    GlossaryEntry(
        key="cadence",
        term="Cadence (streak / missed months)",
        category=Category.DCA,
        definition=(
            "Consistency is DCA's whole engine. Streak = consecutive scheduled months "
            "invested; missed = due months skipped since the plan started."
        ),
        related=["dca"],
        used_in=["trd dca show"],
    ),
    GlossaryEntry(
        key="backtest",
        term="Backtest",
        category=Category.DCA,
        definition=(
            "Replaying a strategy against real history: 'if I had run this exact plan for "
            "the last N years, what would have happened?' Honest only with adjusted closes "
            "and disclosed windows — past performance still doesn't promise the future. "
            "The engine's backtest replays its live entry/exit rules bar by bar; treat its "
            "numbers as an upper bound (survivorship, no slippage or spread)."
        ),
        related=["adjusted-close", "monte-carlo", "benchmark"],
        used_in=["trd dca backtest", "trd engine backtest"],
    ),
    # ── accounts & tax ────────────────────────────────────────────────────
    GlossaryEntry(
        key="rsu",
        term="RSU (restricted stock unit)",
        category=Category.ACCOUNTS_TAX,
        definition=(
            "Employer stock that becomes yours on a vesting schedule. At vest it's taxed as "
            "income at that day's price — which is why cost basis = market value at vest."
        ),
        example=(
            "Your ACME RSUs: each vest date is a lot, basis = vest-day price "
            "(already taxed as income)."
        ),
        related=["espp", "cost-basis"],
        used_in=["trd lots (stock plan)"],
    ),
    GlossaryEntry(
        key="espp",
        term="ESPP (employee stock purchase plan)",
        category=Category.ACCOUNTS_TAX,
        definition=(
            "Buying employer stock through payroll, usually at a discount. A long-held ESPP position "
            "became the positions of several spun-off companies."
        ),
        related=["rsu", "fifo"],
        used_in=["trd lots"],
    ),
    GlossaryEntry(
        key="pdt",
        term="PDT rule (pattern day trader)",
        category=Category.ACCOUNTS_TAX,
        definition=(
            "FINRA rule: 4+ day-trades in 5 business days in a margin account flags you as a "
            "pattern day trader, requiring $25k minimum equity. The rule change you're "
            "watching before Phase 5 day-trading."
        ),
        related=["paper-trading"],
        used_in=["(Phase 5)"],
    ),
    GlossaryEntry(
        key="expense-ratio",
        term="Expense ratio",
        category=Category.ACCOUNTS_TAX,
        definition=(
            "An ETF's annual fee, baked into its price. SPY 0.09%, QQQ 0.20%, SMH 0.35%, "
            "ARKX 0.75% — your $100/month pays roughly 23 cents/year in fees per $100 held "
            "at your weights."
        ),
        formula="annual cost = holding value x expense ratio",
        related=["dca"],
        used_in=["(research — trd quote)"],
    ),
    GlossaryEntry(
        key="total-return",
        term="Total return",
        category=Category.RETURNS,
        definition=(
            "Everything you've made on what you currently hold, as a percent of what "
            "you paid. The headline 'am I up?' number — but it ignores WHEN you invested "
            "(see xirr for the time-aware version)."
        ),
        formula="total return % = (current value - cost basis) / cost basis x 100",
        example="$50,000 invested, now worth $62,500 -> total return = 12,500/50,000 = +25%.",
        related=["pl", "xirr", "cost-basis"],
        used_in=["trd dashboard", "trd portfolio"],
    ),
    GlossaryEntry(
        key="alpha",
        term="Alpha (vs the market)",
        category=Category.RETURNS,
        definition=(
            "How much better (or worse) you did than simply buying the S&P 500 with the "
            "same money on the same days. Positive alpha means your choices added value; "
            "negative means an index fund would have beaten you."
        ),
        formula="alpha = your total return % - S&P 500 same-dates return %",
        example="You +25.4%, S&P 500 +18.2% on the same contributions -> alpha = +7.2pp.",
        related=["benchmark", "total-return", "xirr"],
        used_in=["trd dashboard"],
    ),
    GlossaryEntry(
        key="concentration",
        term="Concentration risk",
        category=Category.RETURNS,
        definition=(
            "How much of your portfolio rides on a single position. One stock at 50%+ "
            "means your results are really a bet on that one company — diversification's "
            "opposite. trd flags a top holding at or above 25%."
        ),
        formula="largest weight = biggest holding value / total portfolio value x 100",
        example="ACME is 54.5% of the portfolio -> a concentrated single-stock bet.",
        related=["allocation", "drift", "rebalancing"],
        used_in=["trd dashboard --full"],
    ),
    GlossaryEntry(
        key="win-rate",
        term="Win rate",
        category=Category.RETURNS,
        definition=(
            "Share of your positions currently in the green. Interesting but secondary: "
            "a portfolio can have a low win rate and still win big if its few winners are "
            "large enough — one big winner can outweigh ten small losers."
        ),
        formula="win rate = positions up / (positions up + positions down) x 100",
        example="18 up, 15 down -> 18/33 = 55%.",
        related=["total-return", "expectancy", "r-multiple"],
        used_in=["trd dashboard --full", "trd engine report", "trd engine backtest"],
    ),
    # ── engine ────────────────────────────────────────────────────────────
    # The trading engine reports in units of risk, not dollars, because a $10
    # day trade and a $1,000 swing trade are only comparable once both are
    # expressed as multiples of what they put at risk.
    GlossaryEntry(
        key="r-multiple",
        term="R-multiple (R)",
        category=Category.ENGINE,
        definition=(
            "A trade's result measured in units of what it risked. 1R is the distance "
            "from entry to the initial stop, so +2R means the trade made twice what it "
            "stood to lose. This is how the engine compares a $10 day trade with a "
            "$1,000 swing trade — dollars would say nothing useful."
        ),
        formula=(
            "1R = entry price - initial stop  (risk per share)\n"
            "R-multiple = (exit price - entry price) / 1R"
        ),
        example=(
            "Bought at 100 with a stop at 95 -> 1R = $5. Sold at 110 -> "
            "R = (110-100)/5 = +2.0R. Stopped out at 95 instead -> -1.0R."
        ),
        related=["expectancy", "initial-stop", "profit-target", "trailing-stop"],
        used_in=["trd engine positions", "trd engine report", "trd engine backtest"],
    ),
    GlossaryEntry(
        key="expectancy",
        term="Expectancy",
        category=Category.ENGINE,
        definition=(
            "The average R a strategy earns per trade — the number to read first on any "
            "scorecard. Above 0 means the rule paid for the risk it took. It beats win "
            "rate because it accounts for size: 40% winners at +2R each is a good "
            "system, while 70% winners that give it all back on the losers is not.\n\n"
            "Expectancy needs a sample before it means anything. With a ~1.2R spread "
            "per trade, telling a 0.2R edge from noise takes roughly 144 trades per "
            "strategy — which is why 'trd engine backtest' exists."
        ),
        formula="expectancy = mean(R-multiple of every closed trade)",
        example=(
            "breakout over 286 backtested trades: +0.29R. Risking $100 a trade, that is "
            "about $29 of edge per trade before costs."
        ),
        related=["r-multiple", "win-rate", "backtest", "survivorship"],
        used_in=["trd engine report", "trd engine backtest"],
    ),
    GlossaryEntry(
        key="initial-stop",
        term="Initial stop",
        category=Category.ENGINE,
        definition=(
            "The price at which the engine admits the trade was wrong, set once at entry "
            "and never moved. Volatility-scaled via ATR, so a jumpy name gets a wider "
            "stop than a calm one and both risk about the same dollars.\n\n"
            "It never moves on purpose: 1R is measured from it, so a stop that drifted "
            "would make every closed trade's R-multiple mean something different and the "
            "scorecard would describe a risk profile the engine is not running."
        ),
        formula="initial stop = entry - stop_atr_mult x ATR(14)   (stop_atr_mult default 2.0)",
        example="Entry 100, ATR(14) = 2.50 -> stop = 100 - 2x2.50 = 95.00, so 1R = $5.",
        related=["r-multiple", "trailing-stop", "atr", "exit-stop"],
        used_in=["trd engine positions", "trd engine rules"],
    ),
    GlossaryEntry(
        key="trailing-stop",
        term="Trailing stop (chandelier)",
        category=Category.ENGINE,
        definition=(
            "A stop that rides up behind the highest close the trade has seen, locking in "
            "gains as they appear. It only takes over once it sits above the initial stop, "
            "so it can tighten risk but never widen it. In 'trd engine positions' an "
            "up-arrow on the stop means the trail is the one in force."
        ),
        formula=(
            "trail stop = highest close since entry - trail_atr_mult x ATR at entry\n"
            "in force only while trail stop > initial stop   (trail_atr_mult default 3.0)"
        ),
        example=(
            "Entry 100, ATR 2.50, initial stop 95. Price peaks at 115 -> trail = "
            "115 - 3x2.50 = 107.50, now above 95, so the trade cannot lose money."
        ),
        related=["initial-stop", "r-multiple", "atr", "exit-trail"],
        used_in=["trd engine positions", "trd engine rules"],
    ),
    GlossaryEntry(
        key="profit-target",
        term="Profit target",
        category=Category.ENGINE,
        definition=(
            "Where the engine takes the win, set as a multiple of the initial risk. A 2R "
            "target only needs to be right slightly more than a third of the time to "
            "break even — that arithmetic, not prediction, is what the engine is built on."
        ),
        formula="target = entry + target_r x (entry - initial stop)   (target_r default 2.0)",
        example="Entry 100, stop 95 -> 1R = $5 -> 2R target = 100 + 2x5 = 110.",
        related=["r-multiple", "initial-stop", "expectancy", "exit-target"],
        used_in=["trd engine positions", "trd engine rules"],
    ),
    GlossaryEntry(
        key="position-sizing",
        term="Position sizing (fixed dollar)",
        category=Category.ENGINE,
        definition=(
            "Every engine trade commits the same dollar amount, so no single name "
            "dominates the book. Share counts are fractional, which matters more than it "
            "sounds: rounding down to whole shares would give a $340 stock $680 of a "
            "$1,000 slot and a $1,278 stock nothing at all, so price alone would "
            "re-weight the portfolio and quietly drop the expensive half of the universe."
        ),
        formula="quantity = position_size / entry price   (rounded down to 6 decimals)",
        example="$1,000 slot, entry 340.12 -> 2.940138 shares, not 2.",
        related=["r-multiple", "paper-trading"],
        used_in=["trd engine init", "trd engine positions"],
    ),
    GlossaryEntry(
        key="max-drawdown",
        term="Maximum drawdown",
        category=Category.ENGINE,
        definition=(
            "The deepest peak-to-trough fall in account value over a period — the worst "
            "stretch you would have had to sit through. The number that decides whether a "
            "strategy is survivable in practice rather than just profitable on paper: a "
            "system returning +300% through a -50% drawdown is one most people abandon "
            "at the bottom."
        ),
        formula="drawdown% at each point = (value - running peak) / running peak x 100\n"
        "max drawdown = the most negative of those",
        example=(
            "Backtested equity peaks at 20,000 then falls to 14,080 before recovering -> "
            "max drawdown = -29.6%."
        ),
        related=["expectancy", "backtest", "equity-curve"],
        used_in=["trd equity", "trd engine backtest"],
    ),
    GlossaryEntry(
        key="earnings-blackout",
        term="Earnings blackout",
        category=Category.ENGINE,
        definition=(
            "The engine refuses new entries when a company reports within the next few "
            "days. A stop bounds the loss at 1R only while price moves continuously; an "
            "overnight earnings gap skips straight past the level, so a trade that "
            "believes it risks 1R can realise several — and because the scorecard averages "
            "R-multiples, a few of those quietly misdescribe the whole system's risk.\n\n"
            "Entries *after* a print stay allowed on purpose: the gap-and-volume day is "
            "exactly what the breakout rule exists to catch. This removes the coin flip, "
            "not the setup it creates."
        ),
        formula="blocked when 0 <= (next earnings date - today) <= earnings_blackout_days"
        "   (default 3)",
        example=(
            "NVDA reports in 2 days and momentum fires -> the signal is logged with its "
            "reason but not taken."
        ),
        related=["r-multiple", "initial-stop", "session-close"],
        used_in=["trd engine scan", "trd engine backtest", "trd earnings"],
    ),
    GlossaryEntry(
        key="session-close",
        term="Flat by the bell (day mode)",
        category=Category.ENGINE,
        definition=(
            "A day-mode engine closes everything before the market shuts, holding no "
            "position overnight. This is the whole difference between a day engine and a "
            "swing engine: swing trades are designed to carry for days, day trades refuse "
            "to hold gap risk at all. Entries also stop 30 minutes before the flat time, "
            "since a fill that gets flattened minutes later just pays the spread twice."
        ),
        formula="flat_at_minute as HHMM in the engine's local time; 0 disables it (swing mode)",
        example="flat_at_minute = 1555 -> flat at 15:55, no new entries after 15:25.",
        related=["earnings-blackout", "exit-session-close", "paper-trading"],
        used_in=["trd engine init --flat-at", "trd engine rules"],
    ),
    GlossaryEntry(
        key="survivorship",
        term="Survivorship bias",
        category=Category.ENGINE,
        definition=(
            "Backtesting today's watchlist over ten years only ever tests the companies "
            "that made it to today. The ones that were obvious buys in 2018 and later "
            "collapsed are simply absent, so results look better than any decision you "
            "could actually have made at the time. It is the main reason a backtest is an "
            "upper bound rather than a forecast — alongside paying no spread or slippage, "
            "and using prices that were retroactively adjusted for splits."
        ),
        example=(
            "A 15-name universe picked in 2026 replayed from 2016 never holds a name that "
            "went to zero in between, because it was never in the list."
        ),
        related=["backtest", "lookahead", "expectancy", "adjusted-close"],
        used_in=["trd engine backtest"],
    ),
    GlossaryEntry(
        key="lookahead",
        term="Lookahead bias",
        category=Category.ENGINE,
        definition=(
            "A backtest accidentally using information that did not exist yet — tomorrow's "
            "close, a later high — when deciding what to do today. It is the most "
            "dangerous bug in this kind of code because it does not look like a bug: it "
            "produces a spectacular edge and reads as success.\n\n"
            "trd guards against it structurally: at each step a strategy is handed only "
            "the bars up to and including that day, and a test rewrites the future to "
            "prove the decisions already made do not change."
        ),
        example=(
            "Checking a stop against a bar's low is honest; deciding to buy because the "
            "*next* bar gapped up is lookahead."
        ),
        related=["backtest", "survivorship", "expectancy"],
        used_in=["trd engine backtest"],
    ),
    GlossaryEntry(
        key="equity-curve",
        term="Equity curve",
        category=Category.ENGINE,
        definition=(
            "Account value plotted over time — cash plus the market value of everything "
            "held, marked at each day's close. The shape matters as much as the endpoint: "
            "two strategies can finish at the same number, one climbing steadily and the "
            "other lurching through drawdowns that would have been hard to hold."
        ),
        formula="value on a day = cash + sum(quantity x that day's close) for every open position",
        example="Starting 5,000 -> ending 19,776 over ten years, with a -29.6% worst stretch.",
        related=["max-drawdown", "xirr", "backtest"],
        used_in=["trd equity", "trd engine backtest"],
    ),
]


def _indicator_entries() -> list[GlossaryEntry]:
    entries = []
    for indicator in INDICATOR_REGISTRY.values():
        entries.append(
            GlossaryEntry(
                key=indicator.key,
                term=indicator.name,
                category=Category.INDICATORS,
                definition=indicator.description,
                related=["pl"],
                used_in=["trd indicators", "trd indicator info " + indicator.key],
            )
        )
    return entries


def _strategy_entries() -> list[GlossaryEntry]:
    """Entry rules, straight from the code registry — the same generated-not-copied
    treatment the indicators get, so a rule's description lives in one place."""
    return [
        GlossaryEntry(
            key=strategy.key.replace("_", "-"),
            term=f"{strategy.name} (entry rule)",
            category=Category.ENGINE,
            definition=strategy.description,
            formula=f"needs {strategy.min_bars} bars of history before it can fire",
            related=["expectancy", "r-multiple", "initial-stop"],
            used_in=["trd engine rules", "trd engine signals", "trd engine report"],
        )
        for strategy in STRATEGY_REGISTRY.values()
    ]


def _exit_entries() -> list[GlossaryEntry]:
    """Exit rules, in the fixed order they are checked. Keys are prefixed because
    bare 'stop' or 'time' are too generic to be useful dictionary entries."""
    return [
        GlossaryEntry(
            key=f"exit-{rule.key.replace('_', '-')}",
            term=f"{rule.name} (exit rule {i} of {len(EXIT_RULES)})",
            category=Category.ENGINE,
            definition=rule.description,
            related=["r-multiple", "initial-stop", "trailing-stop", "profit-target"],
            used_in=["trd engine rules", "trd engine positions"],
        )
        for i, rule in enumerate(EXIT_RULES, start=1)
    ]


GLOSSARY: dict[str, GlossaryEntry] = {
    e.key: e for e in _ENTRIES + _indicator_entries() + _strategy_entries() + _exit_entries()
}


def all_entries() -> list[GlossaryEntry]:
    return sorted(GLOSSARY.values(), key=lambda e: (e.category, e.key))


def lookup(query: str) -> GlossaryEntry | list[GlossaryEntry]:
    """Exact key match, else fuzzy candidates (substring on key or term).

    Underscores normalise to hyphens so the keys trd prints elsewhere resolve as
    typed: `trd engine report` shows `macd_cross`, and looking up exactly what is
    on screen has to work.
    """
    q = query.strip().lower().replace(" ", "-").replace("_", "-")
    if q in GLOSSARY:
        return GLOSSARY[q]
    plain = query.strip().lower()
    candidates = [e for e in all_entries() if plain in e.key.lower() or plain in e.term.lower()]
    return candidates

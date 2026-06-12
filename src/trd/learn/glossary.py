"""The learn-to-invest dictionary: every term trd shows and every formula trd
computes, with worked examples. Numbers on screen -> `trd learn <term>` -> the
exact formula used. Indicator entries are generated from the indicator
registry so descriptions live in one place."""

from enum import StrEnum

from pydantic import BaseModel

from trd.indicators import REGISTRY as INDICATOR_REGISTRY


class Category(StrEnum):
    BASICS = "basics"
    RETURNS = "returns"
    DCA = "dca"
    INDICATORS = "indicators"
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
        example="Paid $1,531 for QQQ, now worth $28,853 -> P&L = +$27,322 (+1,784%).",
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
            "Your DSP position is down $22k unrealized: a real loss only if you sell at this price."
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
        example="$1,418.99 basis / 40 QQQ = $35.47 per share.",
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
        example="BXP pays ~$15 dividend -> broker buys 0.217 more shares that day -> new lot.",
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
            "and disclosed windows — past performance still doesn't promise the future."
        ),
        related=["adjusted-close", "monte-carlo", "benchmark"],
        used_in=["trd dca backtest"],
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
            "Your DSP RSUs: each vest date is a lot, basis = vest-day price "
            "(already taxed as income)."
        ),
        related=["espp", "cost-basis"],
        used_in=["trd lots (etrade-stockplan)"],
    ),
    GlossaryEntry(
        key="espp",
        term="ESPP (employee stock purchase plan)",
        category=Category.ACCOUNTS_TAX,
        definition=(
            "Buying employer stock through payroll, usually at a discount. Your 2012 HP ESPP "
            "became the HPE/HPQ/DXC positions via spinoffs."
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


GLOSSARY: dict[str, GlossaryEntry] = {e.key: e for e in _ENTRIES + _indicator_entries()}


def all_entries() -> list[GlossaryEntry]:
    return sorted(GLOSSARY.values(), key=lambda e: (e.category, e.key))


def lookup(query: str) -> GlossaryEntry | list[GlossaryEntry]:
    """Exact key match, else fuzzy candidates (substring on key or term)."""
    q = query.strip().lower().replace(" ", "-")
    if q in GLOSSARY:
        return GLOSSARY[q]
    plain = query.strip().lower()
    candidates = [e for e in all_entries() if plain in e.key.lower() or plain in e.term.lower()]
    return candidates

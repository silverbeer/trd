"""The allocation plan's daily report, scored against the market (SB-1416).

The view says what the account is worth; this says whether that is good. Every
comparison is "the same dollars, the same days": each benchmark is bought with
exactly the money the account received (or invested) on the day it arrived, so
what differs between two rows is only what the money was put into. That is a
money-weighted, simple comparison — the honest one for an account funded in
stages, where a time-weighted index return would credit the market with months
the money was not in it.

Two bases, kept apart because they answer different questions:

- DEPOSITS — the whole account, cash and all, against every deposit bought into
  an index. A staged plan holding 40% cash by design lags this in a rising
  market and cushions in a falling one, and that is the plan, not a verdict.
- INVESTED — the money in funds against the same invested dollars bought into
  an index, and into the plan's own target mix. This is the "are the funds any
  good" number.

Benchmarks are priced on daily closes without dividends (stored bars where
present, the provider otherwise — never stored, the report runs read-only) and
valued now at the live quote, the same moment the holdings are. A price that is
missing makes a score None, never zero.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from trd.errors import TrdError
from trd.models import Quote, Transaction
from trd.models.alloc import AllocReport, AllocView, BenchmarkScore, FundScore, Score
from trd.services.alloc import AllocService, _invested_targets, add_months
from trd.services.benchmark import same_dates_shares
from trd.services.cashflow import trade_amounts
from trd.services.fifo import fifo_position

# (symbol, label, short name in the message). Broad enough to answer "compared with
# what": US large caps, US tech, the whole world, and a balanced fund.
BENCHMARKS: list[tuple[str, str, str]] = [
    ("SPY", "S&P 500", "S&P 500"),
    ("QQQ", "Nasdaq-100", "Nasdaq"),
    ("VT", "World stocks", "World"),
    ("AOR", "60/40 balanced", "Stocks+bonds"),
]
TARGET_MIX = "target_mix"
# A close counts for a date within this many days of it (weekend/holiday-safe),
# the same window PriceRepo.close_on_or_after uses.
CLOSE_WINDOW_DAYS = 7
# Bars fetched from before the earliest date needed, so an on-or-before lookup
# on a Monday still finds the Friday.
FETCH_PAD_DAYS = 10
PERIODS = ("1W", "1M")
# A quote's session is the newest daily bar on or before today. A series whose
# newest bar is older than the last weekday by more than this (a holiday plus a
# weekend) is stale, and its "previous session" would be some older day's —
# Today is then unknown rather than a two-session move called one.
SESSION_STALE_DAYS = 4

Flows = list[tuple[date, Decimal]]


class Closes:
    """One symbol's daily closes, looked up by date."""

    def __init__(self, bars: dict[date, Decimal]) -> None:
        self.bars = bars
        self.dates = sorted(bars)

    def on_or_after(self, on: date) -> Decimal | None:
        i = bisect_left(self.dates, on)
        if i < len(self.dates) and self.dates[i] <= on + timedelta(days=CLOSE_WINDOW_DAYS):
            return self.bars[self.dates[i]]
        return None

    def on_or_before(self, on: date) -> Decimal | None:
        i = bisect_right(self.dates, on) - 1
        if i >= 0 and self.dates[i] >= on - timedelta(days=CLOSE_WINDOW_DAYS):
            return self.bars[self.dates[i]]
        return None

    def previous_session_close(self, today: date) -> Decimal | None:
        """The close of the session before the one a quote taken `today` belongs
        to: the newest bar on or before today is the quote's session, and the bar
        before it is the previous close. None when either is missing or the
        series is stale — never the provider's own prev_close, which disagreed
        with the real close by 1% on AOR (69.00 vs 68.36, 2026-10-23)."""
        i = bisect_right(self.dates, today) - 1
        if i < 1 or self.dates[i] < last_weekday(today) - timedelta(days=SESSION_STALE_DAYS):
            return None
        return self.bars[self.dates[i - 1]]


def last_weekday(on: date) -> date:
    """`on`, or the Friday before it on a weekend. Holidays are not known here;
    SESSION_STALE_DAYS absorbs them."""
    return on - timedelta(days=max(0, on.weekday() - 4))


def day_move(price: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """Today as a percentage: the price now against the previous session's close.
    The one definition every Today in the report uses — the account, each fund,
    every benchmark and the target mix — so they are measured the same way."""
    if price is None or previous is None or previous == 0:
        return None
    return (price / previous - 1) * 100


def period_starts(funded_on: date | None, today: date) -> dict[str, date]:
    """The windows the account is old enough for: a week once it is seven days
    old, a month once it is a calendar month old. Younger, and the window would
    start before there was any money to measure."""
    if funded_on is None:
        return {}
    starts = {"1W": today - timedelta(days=7), "1M": add_months(today, -1)}
    return {k: s for k, s in starts.items() if funded_on <= s}


def period_return(
    start_value: Decimal | None, end_value: Decimal | None, flowed_in: Decimal
) -> Decimal | None:
    """Simple return over a window with money added inside it, the money counted
    as there from the start: (end - start - added) / (start + added). Crude, and
    applied identically to the account and every benchmark, which is what makes
    the rows comparable."""
    if start_value is None or end_value is None:
        return None
    capital = start_value + flowed_in
    if capital <= 0:
        return None
    return (end_value - start_value - flowed_in) / capital * 100


def _flowed_in(flows: Flows, after: date) -> Decimal:
    return sum((a for on, a in flows if on > after), Decimal(0))


def basket_score(
    flows: Flows,
    weights: dict[str, Decimal],
    closes: dict[str, Closes],
    quotes: dict[str, Quote],
    starts: dict[str, date],
    today: date,
) -> Score:
    """The same flows bought into a basket — one symbol at weight 1, or the
    target mix at its fractions — each leg at its own day's close."""
    base = sum((a for _, a in flows), Decimal(0))
    held: dict[str, Decimal] = {}
    for symbol, weight in weights.items():
        series = closes.get(symbol)
        shares = (
            same_dates_shares([(on, a * weight) for on, a in flows], series.on_or_after)
            if series is not None
            else None
        )
        if shares is None:
            return Score(base=base, value=None)
        held[symbol] = shares
    now = prev = Decimal(0)
    today_known = True
    for symbol, shares in held.items():
        quote = quotes.get(symbol)
        if quote is None:
            return Score(base=base, value=None)
        now += shares * quote.price
        previous = closes[symbol].previous_session_close(today)
        if previous is None:
            today_known = False
        else:
            prev += shares * previous
    periods: dict[str, Decimal | None] = {}
    for label, start in starts.items():
        then: Decimal | None = Decimal(0)
        for symbol, weight in weights.items():
            shares = same_dates_shares(
                [(on, a * weight) for on, a in flows if on <= start],
                closes[symbol].on_or_after,
            )
            close = closes[symbol].on_or_before(start)
            if shares is None or then is None or (shares and close is None):
                then = None
                break
            then += shares * (close or Decimal(0))
        periods[label] = period_return(then, now, _flowed_in(flows, start))
    return Score(
        base=base,
        value=now,
        today_pct=day_move(now, prev) if today_known and prev else None,
        periods=periods,
    )


def _held_value_on(
    txns: list[Transaction], symbols: dict[int, str], closes: dict[str, Closes], on: date
) -> Decimal | None:
    """What the account's holdings were worth at the close on `on`, from FIFO."""
    by_instrument: dict[int, list[Transaction]] = {}
    for t in txns:
        if t.executed_at.date() <= on:
            by_instrument.setdefault(t.instrument_id, []).append(t)
    total = Decimal(0)
    for instrument_id, rows in by_instrument.items():
        quantity, _ = fifo_position(rows)
        if quantity == 0:
            continue
        series = closes.get(symbols[instrument_id])
        close = series.on_or_before(on) if series is not None else None
        if close is None:
            return None
        total += quantity * close
    return total


def _rescore_today(view: AllocView, closes: dict[str, Closes], today: date) -> None:
    """Every held row's Today, and the account's day change, from the previous
    SESSION close in the bars — the same lookup the benchmarks use — instead of
    the quote's prev_close. A row priced from a stale snapshot has no Today, and
    any held row without one leaves the account's day change unknown: a partial
    sum would read as the whole day. Paced targets are rows too, so their own
    PacedRow just takes the row's figure."""
    change: Decimal | None = Decimal(0)
    for r in view.rows + view.other_holdings:
        if r.quantity == 0:
            continue
        price = None if r.price_stale else r.price
        series = closes.get(r.symbol)
        previous = series.previous_session_close(today) if series is not None else None
        r.day_change_pct = day_move(price, previous)
        if change is not None and price is not None and previous is not None:
            change += (price - previous) * r.quantity
        else:
            change = None
    by_symbol = {r.symbol: r.day_change_pct for r in view.rows}
    for p in view.paced:
        p.day_change_pct = by_symbol.get(p.symbol) if p.quantity else None
    view.day_change = change


def _day_pct(change: Decimal | None, now: Decimal | None) -> Decimal | None:
    """The day's change as a share of what it was worth at the previous close."""
    if change is None or now is None:
        return None
    return day_move(now, now - change)


def _funds(view: AllocView) -> list[FundScore]:
    paced = {p.symbol for p in view.paced}
    held = view.holdings_value
    funds = [
        FundScore(
            symbol=r.symbol,
            label=r.label,
            paced=r.symbol in paced,
            quantity=r.quantity,
            value=r.value,
            cost_basis=r.cost_basis,
            today_pct=r.day_change_pct,
            weight_pct=r.value / held * 100 if held and r.value is not None else None,
        )
        for r in view.rows + view.other_holdings
        if r.quantity != 0
    ]
    funds.sort(key=lambda f: (f.gain_pct is None, -(f.gain_pct or Decimal(0))))
    return funds


class AllocReportService:
    """Builds the report from AllocService's view plus benchmark prices. Reads
    only: quotes and bars are fetched, never stored."""

    def __init__(self, alloc: AllocService) -> None:
        self.alloc = alloc
        self.provider = alloc.provider

    def report(self, account_name: str, today: date | None = None) -> AllocReport:
        today = today or date.today()
        view = self.alloc.view(account_name, today)
        plan = view.plan
        deposits: Flows = [
            (f.occurred_at.date(), f.signed) for f in self.alloc.cash.flows_for(account_name)
        ]
        txns = self.alloc.cash.txns.list_chronological(plan.account_id)
        invested_flows = trade_amounts(txns)
        funded_on = min((on for on, _ in deposits), default=None)
        starts = period_starts(funded_on, today)

        mix = {s: w / 100 for s, w in _invested_targets(plan).items()}
        symbols = {s for s, _, _ in BENCHMARKS} | set(mix)
        instruments = self.alloc.portfolio.instruments
        held_symbols: dict[int, str] = {}
        for t in txns:
            if t.instrument_id not in held_symbols:
                instrument = instruments.get(t.instrument_id)
                assert instrument is not None
                held_symbols[t.instrument_id] = instrument.symbol
        symbols |= set(held_symbols.values())
        # Through the last weekday, so the previous SESSION close is in reach even
        # when stored bars stop days ago.
        needed = [on for on, _ in deposits + invested_flows] + list(starts.values())
        needed.append(last_weekday(today))
        closes = self._closes(sorted(symbols), needed, today)
        quotes = self._quotes(sorted(symbols))
        _rescore_today(view, closes, today)

        account = Score(
            base=view.cash.net_deposits,
            value=view.total_value,
            today_pct=_day_pct(view.day_change, view.total_value),
        )
        invested = Score(
            base=sum((a for _, a in invested_flows), Decimal(0)),
            value=view.holdings_value,
            today_pct=_day_pct(view.day_change, view.holdings_value),
        )
        if starts:
            income = self.alloc.cash.income.list_all(plan.account_id)
            for label, start in starts.items():
                held = _held_value_on(txns, held_symbols, closes, start)
                cash = (
                    sum((a for on, a in deposits if on <= start), Decimal(0))
                    - sum((a for on, a in invested_flows if on <= start), Decimal(0))
                    + sum((i.amount for i in income if i.received_at.date() <= start), Decimal(0))
                )
                account.periods[label] = period_return(
                    None if held is None else held + cash,
                    view.total_value,
                    _flowed_in(deposits, start),
                )
                invested.periods[label] = period_return(
                    held, view.holdings_value, _flowed_in(invested_flows, start)
                )

        benchmarks = [
            BenchmarkScore(
                key=symbol,
                label=label,
                short=short,
                symbol=symbol,
                deposits=basket_score(
                    deposits, {symbol: Decimal(1)}, closes, quotes, starts, today
                ),
                invested=basket_score(
                    invested_flows, {symbol: Decimal(1)}, closes, quotes, starts, today
                ),
            )
            for symbol, label, short in BENCHMARKS
        ]
        if mix:
            benchmarks.append(
                BenchmarkScore(
                    key=TARGET_MIX,
                    label="Your target mix",
                    short="Target mix",
                    invested=basket_score(invested_flows, mix, closes, quotes, starts, today),
                )
            )
        spy = benchmarks[0].deposits
        view.benchmark_value = spy.value if spy is not None and deposits else None
        return AllocReport(
            view=view,
            today=today,
            funded_on=funded_on,
            periods=list(starts),
            account=account,
            invested=invested,
            benchmarks=benchmarks,
            funds=_funds(view),
        )

    def _closes(self, symbols: Iterable[str], needed: list[date], today: date) -> dict[str, Closes]:
        """Stored closes where present; the provider fills what they don't reach.
        Nothing fetched is stored — this runs on a read-only connection."""
        start = min(needed) - timedelta(days=FETCH_PAD_DAYS)
        through = max(needed)
        out: dict[str, Closes] = {}
        for symbol in symbols:
            bars: dict[date, Decimal] = {}
            instrument = self.alloc.portfolio.instruments.get_by_symbol(symbol)
            if instrument is not None:
                bars = dict(
                    self.alloc.prices.closes_in_range(instrument.id, start, today, adjusted=False)
                )
            if not bars or max(bars) < through:
                try:
                    fetched = self.provider.get_daily_bars(symbol, start, today + timedelta(days=1))
                except TrdError:
                    fetched = []
                for bar in fetched:
                    bars.setdefault(bar.date, bar.close)
            out[symbol] = Closes(bars)
        return out

    def _quotes(self, symbols: list[str]) -> dict[str, Quote]:
        try:
            return self.provider.get_quotes(symbols)
        except TrdError:
            return {}

"""Sunday Prep — the week-ahead briefing.

Pure logic: it takes a MarketDataProvider and a reference date and returns a fully
structured SundayPrepBriefing. No DuckDB, no Rich, no Typer — the CLI renders it and
(optionally) snapshots it to JSON. The narrative fields (tone, themes, risks, mindset)
are deterministic templates derived from the computed numbers; that keeps trd offline
and testable today, and leaves a clean seam for an --ai pass to rewrite them later.
"""

from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel

from trd.data import (
    COMMODITIES,
    FUTURES,
    INDEX_PROXIES,
    SECTOR_ETFS,
    UNIVERSE,
    VIX_SYMBOL,
    EconEvent,
    events_for_week,
)
from trd.errors import ProviderError
from trd.indicators import math as m
from trd.models import DailyBar
from trd.providers.base import MarketDataProvider

UNUSUAL_MOVE_PCT = Decimal("1.0")  # futures move flagged as outsized
COMMODITY_UNUSUAL_PCT = Decimal("2.0")  # oil/gold are noisier; flag bigger moves
WATCHLIST_MAX = 10
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

MINDSET = (
    "Have a plan before the market opens. Know what you'll do if you're right, and know "
    "what you'll do if you're wrong. The goal isn't to predict the future — it's to respond "
    "intelligently. Patience and position sizing beat prediction."
)
PROMPT_QUESTION = "What is one thing you're watching closely this week?"


# --- structured briefing ----------------------------------------------------


class FuturesQuote(BaseModel):
    label: str
    symbol: str
    price: Decimal | None = None
    change_pct: Decimal | None = None
    unusual: bool = False


class EarningsItem(BaseModel):
    symbol: str
    name: str
    date: date
    day: str
    timing: str  # "BMO" | "AMC" | "TBD" — yfinance rarely gives session, so usually TBD
    why: str


class SectorMove(BaseModel):
    symbol: str
    name: str
    week_pct: Decimal | None = None


class VolatilityRead(BaseModel):
    vix: Decimal | None = None
    band: str
    note: str


class KeyLevel(BaseModel):
    symbol: str
    price: Decimal | None = None
    sma50: Decimal | None = None
    sma200: Decimal | None = None
    high52: Decimal | None = None
    low52: Decimal | None = None
    atr: Decimal | None = None
    note: str = ""


class Theme(BaseModel):
    title: str
    why: str


class WatchItem(BaseModel):
    symbol: str
    rationale: str
    catalyst: str


class SundayPrepBriefing(BaseModel):
    generated_for: date
    week_start: date
    week_end: date
    tone: str
    futures: list[FuturesQuote]
    commodities: list[FuturesQuote]  # oil (WTI/Brent) + gold; reuses the quote-row shape
    econ_events: list[EconEvent]
    earnings: list[EarningsItem]
    sector_leaders: list[SectorMove]
    sector_laggards: list[SectorMove]
    volatility: VolatilityRead
    key_levels: list[KeyLevel]
    themes: list[Theme]
    watchlist: list[WatchItem]
    risks: list[str]
    mindset: str = MINDSET
    prompt_question: str = PROMPT_QUESTION


# --- helpers ----------------------------------------------------------------


def _dec(value: float | None, places: str = "0.01") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places))


def _bar_closes(bars: list[DailyBar]) -> list[float]:
    """Adjusted closes when available — analytics prefer them."""
    return [float(b.adj_close if b.adj_close is not None else b.close) for b in bars]


class SundayPrepService:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    # -- provider access (never raises; missing data degrades to None) --------

    def _safe_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        try:
            return self.provider.get_daily_bars(symbol, start, end)
        except ProviderError:
            return []

    def _safe_quote_pct(self, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        try:
            q = self.provider.get_quote(symbol)
        except ProviderError:
            return None, None
        return q.price, q.day_change_pct

    # -- week window ----------------------------------------------------------

    @staticmethod
    def _week_window(reference: date) -> tuple[date, date]:
        """The Mon-Fri trading week we're prepping for. On a weekend, the next one."""
        wd = reference.weekday()
        if wd >= 5:  # Sat/Sun -> coming Monday
            week_start = reference + timedelta(days=7 - wd)
        else:  # weekday -> this week's Monday
            week_start = reference - timedelta(days=wd)
        return week_start, week_start + timedelta(days=4)

    # -- sections -------------------------------------------------------------

    def _quote_rows(self, symbols: dict[str, str], threshold: Decimal) -> list[FuturesQuote]:
        out: list[FuturesQuote] = []
        for label, symbol in symbols.items():
            price, pct = self._safe_quote_pct(symbol)
            out.append(
                FuturesQuote(
                    label=label,
                    symbol=symbol,
                    price=_dec(float(price)) if price is not None else None,
                    change_pct=pct.quantize(Decimal("0.01")) if pct is not None else None,
                    unusual=pct is not None and abs(pct) >= threshold,
                )
            )
        return out

    def _futures(self) -> list[FuturesQuote]:
        return self._quote_rows(FUTURES, UNUSUAL_MOVE_PCT)

    def _commodities(self) -> list[FuturesQuote]:
        return self._quote_rows(COMMODITIES, COMMODITY_UNUSUAL_PCT)

    def _sectors(self, reference: date) -> tuple[list[SectorMove], list[SectorMove]]:
        # ~2 weeks of calendar days to guarantee a full trading week of bars.
        start = reference - timedelta(days=14)
        end = reference + timedelta(days=1)
        moves: list[SectorMove] = []
        for symbol, name in SECTOR_ETFS.items():
            closes = _bar_closes(self._safe_bars(symbol, start, end))
            pct: Decimal | None = None
            if len(closes) >= 2:
                base = closes[-min(6, len(closes))]  # ~5 trading days back
                if base:
                    pct = _dec((closes[-1] - base) / base * 100)
            moves.append(SectorMove(symbol=symbol, name=name, week_pct=pct))
        ranked = [mv for mv in moves if mv.week_pct is not None]
        ranked.sort(key=lambda mv: mv.week_pct, reverse=True)  # type: ignore[arg-type, return-value]
        leaders = ranked[:3]
        laggards = list(reversed(ranked[-3:])) if len(ranked) >= 3 else []
        return leaders, laggards

    def _volatility(self) -> VolatilityRead:
        price, _ = self._safe_quote_pct(VIX_SYMBOL)
        vix = _dec(float(price)) if price is not None else None
        if vix is None:
            return VolatilityRead(vix=None, band="unknown", note="VIX unavailable this run.")
        v = float(vix)
        if v < 13:
            band, note = (
                "low — complacency",
                (
                    "Cheap hedges and little fear priced in. Calm can persist, but crowded "
                    "complacency is where sharp pullbacks start."
                ),
            )
        elif v < 17:
            band, note = (
                "low-to-normal — calm",
                ("Orderly conditions. Trends tend to grind; breakouts are more trustworthy."),
            )
        elif v < 22:
            band, note = (
                "normal",
                ("Typical two-way risk. No special edge from volatility either direction."),
            )
        elif v < 30:
            band, note = (
                "elevated — rising uncertainty",
                ("Bigger daily ranges. Size positions down; expect headlines to swing the tape."),
            )
        else:
            band, note = (
                "extreme — fear",
                (
                    "Capitulation territory. Historically near-term bottoms form here, but only "
                    "after the selling exhausts. Defense first."
                ),
            )
        return VolatilityRead(vix=vix, band=band, note=note)

    def _key_levels(self, reference: date) -> list[KeyLevel]:
        start = reference - timedelta(days=420)  # >1y of trading bars
        end = reference + timedelta(days=1)
        levels: list[KeyLevel] = []
        for symbol in INDEX_PROXIES:
            bars = self._safe_bars(symbol, start, end)
            closes = _bar_closes(bars)
            if not closes:
                levels.append(KeyLevel(symbol=symbol, note="no price history — run 'trd sync'"))
                continue
            price = closes[-1]
            sma50 = m.sma(closes, 50)[-1]
            sma200 = m.sma(closes, 200)[-1]
            window = bars[-252:]
            high52 = max((float(b.high) for b in window), default=None)
            low52 = min((float(b.low) for b in window), default=None)
            atr = m.atr(
                [float(b.high) for b in bars],
                [float(b.low) for b in bars],
                closes,
                14,
            )[-1]
            levels.append(
                KeyLevel(
                    symbol=symbol,
                    price=_dec(price),
                    sma50=_dec(sma50),
                    sma200=_dec(sma200),
                    high52=_dec(high52),
                    low52=_dec(low52),
                    atr=_dec(atr),
                    note=self._level_note(price, sma50, sma200, high52, low52),
                )
            )
        return levels

    @staticmethod
    def _level_note(
        price: float,
        sma50: float | None,
        sma200: float | None,
        high52: float | None,
        low52: float | None,
    ) -> str:
        parts: list[str] = []
        if sma200 is not None:
            side = "above" if price >= sma200 else "below"
            parts.append(
                f"{side} the 200-day — {'uptrend intact' if side == 'above' else 'caution'}"
            )
        if high52 is not None and high52 > 0 and price >= high52 * 0.98:
            parts.append("within 2% of 52-week highs — breakout watch")
        elif low52 is not None and low52 > 0 and price <= low52 * 1.05:
            parts.append("near 52-week lows — support test")
        if sma50 is not None:
            parts.append(f"50-day ~{sma50:,.0f} is first support/resistance")
        return "; ".join(parts) if parts else "mid-range"

    def _earnings(self, week_start: date, week_end: date) -> list[EarningsItem]:
        out: list[EarningsItem] = []
        for symbol, (name, _theme) in UNIVERSE.items():
            try:
                dates = self.provider.get_earnings_dates(symbol)
            except ProviderError:
                continue
            for ed in dates:
                if week_start <= ed.date <= week_end:
                    out.append(
                        EarningsItem(
                            symbol=symbol,
                            name=name,
                            date=ed.date,
                            day=WEEKDAY_NAMES[ed.date.weekday()],
                            timing="TBD",
                            why=f"Broad-impact {UNIVERSE[symbol][1]} name; read shapes the group.",
                        )
                    )
                    break
        out.sort(key=lambda e: (e.date, e.symbol))
        return out

    def _themes(
        self,
        leaders: list[SectorMove],
        events: list[EconEvent],
        earnings: list[EarningsItem],
        vol: VolatilityRead,
    ) -> list[Theme]:
        themes: list[Theme] = []
        if any("FOMC" in e.name for e in events):
            themes.append(
                Theme(
                    title="Interest-rate expectations",
                    why="An FOMC decision lands this week — the dot plot reprices everything.",
                )
            )
        elif any(k in e.name for e in events for k in ("CPI", "PPI", "PCE", "Payrolls")):
            themes.append(
                Theme(
                    title="Inflation & rate path",
                    why="A top-tier macro print this week feeds directly into rate expectations.",
                )
            )
        if leaders:
            top = leaders[0]
            themes.append(
                Theme(
                    title=f"{top.name} leadership",
                    why=(
                        f"{top.name} led last week ({top.week_pct:+}%); "
                        "watch for rotation to continue or fade."
                    ),
                )
            )
        earn_themes = {UNIVERSE[e.symbol][1] for e in earnings}
        if "AI/Tech" in earn_themes or "Semiconductors" in earn_themes:
            themes.append(
                Theme(
                    title="AI infrastructure spending",
                    why="Mega-cap tech/semis report — capex guidance drives the whole complex.",
                )
            )
        if vol.vix is not None and vol.vix >= 22:
            themes.append(
                Theme(
                    title="Elevated volatility",
                    why=f"VIX at {vol.vix} signals {vol.band}; risk management trumps conviction.",
                )
            )
        if len(earnings) >= 8:
            themes.append(
                Theme(
                    title="Earnings dispersion",
                    why=f"{len(earnings)} notable reports — single-name moves, not just the index.",
                )
            )
        if not themes:
            themes.append(
                Theme(
                    title="Trend continuation",
                    why="Quiet calendar — last week's leadership and prevailing trend carry over.",
                )
            )
        return themes[:5]

    def _risks(
        self,
        events: list[EconEvent],
        earnings: list[EarningsItem],
        vol: VolatilityRead,
        futures: list[FuturesQuote],
    ) -> list[str]:
        risks: list[str] = []
        if any("FOMC" in e.name for e in events):
            risks.append(
                "FOMC decision — a hawkish surprise or hawkish dots can reprice risk fast."
            )
        big = [
            e.name for e in events if any(k in e.name for k in ("CPI", "PPI", "PCE", "Payrolls"))
        ]
        if big:
            risks.append(
                f"Market-moving data: {', '.join(sorted(set(big)))} — hot prints lift rates."
            )
        if len(earnings) >= 5:
            risks.append(
                f"Earnings uncertainty — {len(earnings)} notable reporters; gaps cut both ways."
            )
        if vol.vix is not None and vol.vix >= 22:
            risks.append(f"Elevated volatility (VIX {vol.vix}) — wider ranges, smaller size.")
        if any(f.unusual for f in futures):
            risks.append(
                "Outsized futures move into the open — gaps can fade; don't chase strength."
            )
        risks.append("Concentrated positioning — a few mega-caps drive the index; breadth matters.")
        return risks

    def _watchlist(
        self,
        leaders: list[SectorMove],
        laggards: list[SectorMove],
        earnings: list[EarningsItem],
        key_levels: list[KeyLevel],
        vol: VolatilityRead,
    ) -> list[WatchItem]:
        items: list[WatchItem] = []
        seen: set[str] = set()

        def add(symbol: str, rationale: str, catalyst: str) -> None:
            if symbol in seen or len(items) >= WATCHLIST_MAX:
                return
            seen.add(symbol)
            items.append(WatchItem(symbol=symbol, rationale=rationale, catalyst=catalyst))

        if leaders:
            top = leaders[0]
            add(
                top.symbol,
                f"Top sector last week ({top.name}, {top.week_pct:+}%)",
                "Momentum / rotation continuation",
            )
        if laggards:
            bot = laggards[0]
            add(
                bot.symbol,
                f"Weakest sector ({bot.name}, {bot.week_pct:+}%)",
                "Mean-reversion bounce or fresh breakdown",
            )
        for e in earnings[:4]:
            add(e.symbol, f"{e.name} reports this week", f"Earnings {e.day}")
        for lvl in key_levels:
            if (
                lvl.high52 is not None
                and lvl.price is not None
                and lvl.price >= lvl.high52 * Decimal("0.98")
            ):
                add(lvl.symbol, "Pressing 52-week highs", "Breakout confirmation or rejection")
        if vol.vix is not None and vol.vix >= 22:
            add("VIX", f"Elevated at {vol.vix}", "Hedge demand / fear unwind")
        # Backfill with index proxies so there's always a usable list.
        for symbol in INDEX_PROXIES:
            add(symbol, "Broad-market gauge", "Reaction to the week's macro and key levels")
        return items

    def _tone(
        self,
        futures: list[FuturesQuote],
        vol: VolatilityRead,
        leaders: list[SectorMove],
        events: list[EconEvent],
    ) -> str:
        pcts = [float(f.change_pct) for f in futures if f.change_pct is not None]
        avg = sum(pcts) / len(pcts) if pcts else 0.0
        if avg >= 0.3:
            lead = "Futures point higher into the new week"
        elif avg <= -0.3:
            lead = "Futures point lower, a risk-off lean into the open"
        else:
            lead = "Futures are little changed, a wait-and-see tone"
        clauses = [lead]
        if vol.vix is not None:
            clauses[-1] += f" with the VIX at {vol.vix} ({vol.band})."
        else:
            clauses[-1] += "."
        if leaders:
            clauses.append(f"{leaders[0].name} led last week; watch whether that leadership holds.")
        headline = next((e for e in events if "FOMC" in e.name), None) or next(
            (e for e in events if any(k in e.name for k in ("CPI", "PPI", "PCE", "Payrolls"))),
            None,
        )
        if headline is not None:
            clauses.append(
                f"{headline.name} on {headline.day} is the event that can set the week's direction."
            )
        else:
            clauses.append("A light macro calendar puts the focus on earnings and price action.")
        return " ".join(clauses)

    # -- entry point ----------------------------------------------------------

    def build(self, reference: date) -> SundayPrepBriefing:
        week_start, week_end = self._week_window(reference)
        futures = self._futures()
        commodities = self._commodities()
        leaders, laggards = self._sectors(reference)
        vol = self._volatility()
        key_levels = self._key_levels(reference)
        events = events_for_week(week_start, week_end)
        earnings = self._earnings(week_start, week_end)
        themes = self._themes(leaders, events, earnings, vol)
        risks = self._risks(events, earnings, vol, futures)
        watchlist = self._watchlist(leaders, laggards, earnings, key_levels, vol)
        tone = self._tone(futures, vol, leaders, events)
        return SundayPrepBriefing(
            generated_for=reference,
            week_start=week_start,
            week_end=week_end,
            tone=tone,
            futures=futures,
            commodities=commodities,
            econ_events=events,
            earnings=earnings,
            sector_leaders=leaders,
            sector_laggards=laggards,
            volatility=vol,
            key_levels=key_levels,
            themes=themes,
            watchlist=watchlist,
            risks=risks,
        )

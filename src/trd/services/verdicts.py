"""Each closed trade in a sentence a beginner can read: did the buy find a move,
and did the exit get paid for it.

The review deliberately draws its *conclusions* about rules and never about one
trade — a good decision loses money often enough that judging trades one at a
time teaches the wrong lesson. This module does not break that rule. It
describes, it does not conclude: it turns the four numbers `outcomes.py` already
stores into plain English about what the price did, and says nothing about
whether taking the trade was wise.

The distinction is the whole design:

- "went against us from the first bar and never showed a profit" is an
  observation about a price path, true whatever anyone thinks of the rule.
- "that was a bad buy" is a claim about a decision, needs a population to
  support it, and belongs in a `Finding` with an `n` beside it.

So the labels here are about the *trade*, and the findings above them are about
the *rules*. A reader who wants to know why a loser lost gets an answer; a
reader who wants to know what to change is still pointed at the population.

Four inputs, all in R (one R = the dollars this trade had at risk between its
entry and its stop, so trades of different sizes are comparable):

- **MFE** — the best it ever looked. This is what grades the *buy*: if the move
  the rule was hunting never appeared, the entry found nothing, whatever the
  exit did afterwards.
- **MAE** — the worst it ever looked. A winner that first fell most of the way
  to its stop was a rough entry that recovered, and only MAE separates it from
  a clean one.
- **capture** — the share of the best that was kept. Grades the *exit*.
- **follow-through** — where price went after the exit. The other half of the
  exit's grade, and the only one that can say "we sold too soon".

Every threshold is a module constant for the same reason the detectors' are: a
label tuned per engine is a model with extra steps.
"""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from trd.engine.exits import StopLoss
from trd.models import TradeOutcome

# -- Entry thresholds, in R -------------------------------------------------
#
# A full R is the bar for "the move happened": the engine's own target sits at
# 2R, so a trade that was never offered even one R never came close to the thing
# the rule said it was looking for.
GOOD_MFE_R = Decimal(1)
# Half an R: it went the right way, just never far enough to pay for the risk.
WEAK_MFE_R = Decimal("0.5")
# How much heat a trade can take and still be called a clean entry. Half the
# distance to the stop — past that the entry was early even if it recovered.
ROUGH_MAE_R = Decimal("-0.5")

# -- Exit thresholds --------------------------------------------------------
#
# A whole R of further movement after the exit. Same bar `TradeOutcome.
# exited_early` uses, so the label and the flag can never disagree.
EARLY_FT_R = Decimal(1)
# Price fell this far after we were out: the exit was the reason we did not.
SAVED_FT_R = Decimal("-0.5")
# Kept more than two thirds of the best on offer.
GOOD_CAPTURE = Decimal("0.70")
# Kept less than two fifths of it.
LATE_CAPTURE = Decimal("0.40")
# The exit rule whose wording differs: a stop that is run past is a stop set too
# close, which is a different thing to tell a reader than "we sold too soon".
# Taken from the registry rather than spelled out — the key is "stop", a literal
# "stop_loss" here matched nothing and silently gave every stop the generic
# wording.
STOP_RULE = StopLoss.key

# Capture over a trade that was barely offered anything is a ratio with a
# meaningless denominator — +0.02R peak and a -1R stop scores -5000%. Same
# guard the renderer uses before printing a capture at all.
MIN_MFE_FOR_CAPTURE = Decimal("0.25")


class EntryVerdict(StrEnum):
    """What the *buy* found. About the move, never about the decision."""

    GOOD = "GOOD"  # the move the rule was hunting showed up, cleanly
    ROUGH = "ROUGH"  # the move showed up, but only after going against us first
    WEAK = "WEAK"  # it went our way and ran out of road before a full R
    BAD = "BAD"  # no move: it never went anywhere worth having
    UNKNOWN = "UNKNOWN"  # not enough stored bars to say


class ExitVerdict(StrEnum):
    """What the *sell* did with what the buy found."""

    GOOD = "GOOD"  # kept most of the move, or got out before a fall
    EARLY = "EARLY"  # price carried on without us by a full R or more
    LATE = "LATE"  # handed back most of a move it had already been offered
    OK = "OK"  # neither notable — the ordinary case
    UNKNOWN = "UNKNOWN"


class TradeVerdict(BaseModel):
    """One trade described twice: what the buy found, what the exit kept.

    `entry_note` and `exit_note` are the whole point — the label is a filing
    category and the sentence is what a reader actually learns from. Both carry
    their own numbers so a line stands alone in a Telegram message with no table
    above it to look up.
    """

    entry: EntryVerdict
    entry_note: str
    exit: ExitVerdict
    exit_note: str


def _r(value: Decimal | None) -> str:
    """An R-multiple as a reader sees it elsewhere in trd: signed, 2dp, suffixed."""
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}{abs(value):.2f}R"


def _amount(value: Decimal | None) -> str:
    """An unsigned R amount, for a quantity whose direction is already in the
    sentence: "1.76R handed back" reads as English, "+1.76R handed back" does not."""
    return "—" if value is None else f"{abs(value):.2f}R"


def _pct(value: Decimal) -> str:
    return f"{value * 100:.0f}%"


def _bar_width(timeframe: str) -> str:
    """ "5-minute", "hourly", "daily" — the bar width as a reader says it aloud."""
    if timeframe == "1d":
        return "daily"
    if timeframe.endswith("m"):
        return f"{timeframe[:-1]}-minute"
    if timeframe.endswith("h"):
        return f"{timeframe[:-1]}-hour"
    return timeframe


def entry_verdict(outcome: TradeOutcome) -> tuple[EntryVerdict, str]:
    """Grade the buy on what the trade was ever *offered*, not on what it made.

    An entry cannot be held responsible for an exit: a trade offered +3R and
    booked at -0.2R found exactly the move it was looking for. Reading MFE and
    nothing else is what keeps the two grades independent, so "good buy, bad
    exit" is a thing this can actually say.
    """
    mfe = outcome.mfe_r
    if mfe is None:
        return EntryVerdict.UNKNOWN, "no stored outcome for this trade — nothing to measure"
    if outcome.bars_seen == 0:
        # Opened and closed inside a single bar. The endpoints are known and the
        # path between them is not, so MFE here is the exit price wearing a
        # different name. Grading a buy on it would say "we were offered exactly
        # what we took" about every such trade — a measurement that can only
        # ever agree with itself.
        return (
            EntryVerdict.UNKNOWN,
            f"opened and closed inside one {_bar_width(outcome.timeframe)} bar — the engine "
            "stores no price path through it, so there is nothing to say about what the buy "
            "was offered",
        )

    mae = outcome.mae_r if outcome.mae_r is not None else Decimal(0)
    heat = "never traded below what we paid" if mae >= 0 else f"the worst it looked was {_r(mae)}"

    if mfe >= GOOD_MFE_R:
        if mae <= ROUGH_MAE_R:
            return (
                EntryVerdict.ROUGH,
                f"the move did happen — it was offered {_r(mfe)} — but it fell {_r(mae)} "
                "against us first, which is a good trade bought early",
            )
        return (
            EntryVerdict.GOOD,
            f"the move the rule was looking for happened: offered {_r(mfe)}, and {heat}",
        )
    if mfe >= WEAK_MFE_R:
        return (
            EntryVerdict.WEAK,
            f"it went our way but ran out of road early — the best it ever offered was "
            f"{_r(mfe)}, under the one full R the trade was risking",
        )
    return (
        EntryVerdict.BAD,
        f"no move to catch — the best it ever offered was {_r(mfe)}, under half the distance "
        f"to its own stop, and {heat}",
    )


def exit_verdict(outcome: TradeOutcome, rule: str | None = None) -> tuple[ExitVerdict, str]:
    """Grade the sell on two things the buy cannot be blamed for: how much of the
    move was kept, and what price did once we were out.

    Order of tests is the order of evidence strength. Price carrying on without
    us by a whole R is the most direct answer to "did we sell too soon" there is,
    so it is asked first; giving back a move we had already been handed is the
    next; and what happened after a stop only means anything once neither of
    those applies.
    """
    exit_r = outcome.exit_r
    mfe = outcome.mfe_r
    capture = outcome.capture
    seen = outcome.follow_through_seen or 0
    after = outcome.follow_through_r if seen else None
    stopped = rule == STOP_RULE

    measurable = capture if (mfe is not None and mfe >= MIN_MFE_FOR_CAPTURE) else None

    if after is not None and after >= EARLY_FT_R:
        sold = f"we sold at {_r(exit_r)}" if exit_r is not None else "we sold"
        if stopped:
            return (
                ExitVerdict.EARLY,
                f"the stop fired and then price turned: it ran {_r(after)} past where we "
                "were sold out, so the stop sat too close to the entry",
            )
        return (
            ExitVerdict.EARLY,
            f"{sold} and price carried on another {_r(after)} without us",
        )

    if measurable is not None and measurable < LATE_CAPTURE and mfe is not None:
        given = outcome.gave_back_r
        if measurable < 0:
            # Capture below zero is not "kept a negative share of the move" — it
            # is a trade that was up and closed down. Printing "-188% kept" is
            # arithmetically true and tells a reader nothing they can use.
            return (
                ExitVerdict.LATE,
                f"it was offered {_r(mfe)} and still closed at a loss of {_r(exit_r)} — the "
                f"whole move, {_amount(given)}, was handed back before the exit fired",
            )
        return (
            ExitVerdict.LATE,
            f"it was offered {_r(mfe)} and we booked {_r(exit_r)} — {_amount(given)} handed "
            f"back before the exit fired, only {_pct(measurable)} of the move kept",
        )

    if after is not None and after <= SAVED_FT_R:
        did = "the stop did its job" if stopped else "the exit did its job"
        return (
            ExitVerdict.GOOD,
            f"{did}: price fell another {_r(after)} after we were out",
        )

    if measurable is not None and measurable >= GOOD_CAPTURE:
        return (
            ExitVerdict.GOOD,
            f"kept {_pct(measurable)} of the best this trade was ever offered ({_r(mfe)})",
        )

    if measurable is None and after is None:
        return ExitVerdict.UNKNOWN, "nothing stored after the exit yet — ask again in a few days"

    kept = f"kept {_pct(measurable)} of what was offered" if measurable is not None else None
    drift = f"price went {_r(after)} after we left" if after is not None else None
    return ExitVerdict.OK, "; ".join(p for p in (kept, drift) if p) or "nothing notable either way"


def verdict(outcome: TradeOutcome | None, rule: str | None = None) -> TradeVerdict | None:
    """Both halves, or None when the trade was never measured.

    None rather than a row of UNKNOWNs: a trade with no stored outcome has not
    been backfilled, and printing two shrugs beside it reads as a measurement
    that came back empty rather than one that was never taken.
    """
    if outcome is None:
        return None
    entry, entry_note = entry_verdict(outcome)
    exit_, exit_note = exit_verdict(outcome, rule)
    return TradeVerdict(entry=entry, entry_note=entry_note, exit=exit_, exit_note=exit_note)

"""The per-trade grades: every boundary, and the lines that must never be crossed.

Two kinds of test here. Most are boundary tests — a threshold that drifts by a
tenth of an R silently reclassifies a third of the book, and a label whose
meaning moves is worse than no label. The rest guard the thing the module exists
to do: grade the buy and the exit *independently*, so "good buy, bad exit" is
sayable, and never turn a description of a price path into a verdict on a
decision.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from trd.models import TradeOutcome
from trd.services.verdicts import (
    EntryVerdict,
    ExitVerdict,
    entry_verdict,
    exit_verdict,
    verdict,
)


def outcome(
    mfe: str | None = "2.0",
    mae: str = "-0.2",
    exit_r: str = "1.8",
    follow: str | None = None,
    bars: int = 10,
    timeframe: str = "5m",
) -> TradeOutcome:
    """A measured trade whose every number is chosen, not sampled.

    `capture` is derived here exactly as `outcomes.trade_outcome` derives it, so
    a test that sets an exit and an MFE cannot accidentally describe a trade the
    engine could never produce.
    """
    mfe_r = Decimal(mfe) if mfe is not None else None
    exit_dec = Decimal(exit_r)
    return TradeOutcome(
        position_id=1,
        computed_at=datetime(2026, 9, 11, 16, 0),
        timeframe=timeframe,
        bars_seen=bars,
        risk_per_share=Decimal("10"),
        mae_r=Decimal(mae),
        mfe_r=mfe_r,
        exit_r=exit_dec,
        capture=(exit_dec / mfe_r) if mfe_r is not None and mfe_r > 0 else None,
        follow_through_r=Decimal(follow) if follow is not None else None,
        follow_through_bars=12,
        follow_through_seen=12 if follow is not None else 0,
    )


# ------------------------------------------------------------------- the buy


@pytest.mark.parametrize(
    "mfe,mae,expected",
    [
        # A full R is the bar for "the move happened" — the engine's own target
        # is 2R, so a trade never offered one R never came near what it was for.
        ("1.00", "-0.2", EntryVerdict.GOOD),
        ("0.99", "-0.2", EntryVerdict.WEAK),
        ("5.00", "0.0", EntryVerdict.GOOD),
        # Half the distance to the stop is where a clean entry becomes an early
        # one. The move still happened; it was bought before it started.
        ("2.00", "-0.49", EntryVerdict.GOOD),
        ("2.00", "-0.50", EntryVerdict.ROUGH),
        ("2.00", "-0.95", EntryVerdict.ROUGH),
        # Went the right way, never far enough to pay for the risk.
        ("0.50", "-0.1", EntryVerdict.WEAK),
        ("0.49", "-0.1", EntryVerdict.BAD),
        ("0.00", "-2.4", EntryVerdict.BAD),
    ],
)
def test_entry_boundaries(mfe: str, mae: str, expected: EntryVerdict) -> None:
    grade, note = entry_verdict(outcome(mfe=mfe, mae=mae))
    assert grade is expected
    assert note


def test_entry_is_blind_to_the_result() -> None:
    """A buy is graded on what it was offered, never on what it made.

    This is the whole reason the two grades are computed separately. A trade
    offered +3R and handed back at a loss found exactly the move its rule was
    hunting, and a review that let the loss colour the entry grade could never
    say so.
    """
    won = entry_verdict(outcome(mfe="3.0", mae="-0.1", exit_r="2.9"))
    lost = entry_verdict(outcome(mfe="3.0", mae="-0.1", exit_r="-1.0"))
    assert won[0] is lost[0] is EntryVerdict.GOOD
    assert won[1] == lost[1]


def test_entry_unknown_inside_a_single_bar() -> None:
    """No stored path means no grade — and the note says which bar width.

    MFE on a trade that opened and closed inside one bar is the exit price under
    another name. Grading the buy on it would report "we were offered exactly
    what we took" about every scalp, which is a measurement agreeing with
    itself rather than saying anything.
    """
    grade, note = entry_verdict(outcome(mfe="2.1", bars=0, timeframe="5m"))
    assert grade is EntryVerdict.UNKNOWN
    assert "5-minute" in note


def test_entry_unknown_without_an_outcome() -> None:
    grade, _ = entry_verdict(outcome(mfe=None))
    assert grade is EntryVerdict.UNKNOWN


def test_entry_note_distinguishes_no_heat_from_some() -> None:
    clean = entry_verdict(outcome(mfe="2.0", mae="0.0"))[1]
    bruised = entry_verdict(outcome(mfe="2.0", mae="-0.3"))[1]
    assert "never traded below what we paid" in clean
    assert "-0.30R" in bruised


# ------------------------------------------------------------------ the exit


def test_exit_early_beats_every_other_test() -> None:
    """Price carrying on without us is the most direct evidence there is that
    the exit was premature, so it is asked first — even on a trade that kept
    all of its move and would otherwise grade GOOD."""
    grade, note = exit_verdict(outcome(mfe="2.0", exit_r="2.0", follow="1.0"))
    assert grade is ExitVerdict.EARLY
    assert "+1.00R" in note


def test_the_stop_rule_key_comes_from_the_registry() -> None:
    """The key is "stop". A literal "stop_loss" here matched nothing and gave
    every stopped-out trade the generic wording — a bug that is invisible in a
    unit test written against the same wrong string."""
    from trd.engine import EXIT_REGISTRY
    from trd.services.verdicts import STOP_RULE

    assert STOP_RULE in EXIT_REGISTRY


def test_exit_early_on_a_stop_names_the_stop() -> None:
    """A stop that fires and is then run past is a stop set too close, which is
    a different thing to tell a reader than 'we sold too soon'."""
    _, note = exit_verdict(outcome(mfe="0.0", exit_r="-1.0", follow="2.0"), rule="stop")
    assert "too close" in note


def test_exit_early_boundary() -> None:
    assert exit_verdict(outcome(exit_r="1.8", follow="0.99"))[0] is not ExitVerdict.EARLY
    assert exit_verdict(outcome(exit_r="1.8", follow="1.00"))[0] is ExitVerdict.EARLY


def test_exit_late_boundary() -> None:
    # capture = exit / mfe. 0.79/2.0 = 39.5% -> LATE; 0.80/2.0 = 40% -> not.
    assert exit_verdict(outcome(mfe="2.0", exit_r="0.79"))[0] is ExitVerdict.LATE
    assert exit_verdict(outcome(mfe="2.0", exit_r="0.80"))[0] is not ExitVerdict.LATE


def test_exit_late_on_a_loss_does_not_print_a_negative_percentage() -> None:
    """A trade that was up and closed down did not keep "-188% of the move".

    The arithmetic is right and the sentence is useless, so a capture below zero
    is described instead of quoted.
    """
    grade, note = exit_verdict(outcome(mfe="1.17", exit_r="-0.59"))
    assert grade is ExitVerdict.LATE
    assert "%" not in note
    assert "closed at a loss" in note
    assert "1.76R" in note


def test_exit_good_when_price_fell_after_us() -> None:
    grade, note = exit_verdict(outcome(mfe="0.0", exit_r="-1.0", follow="-1.6"), rule="stop")
    assert grade is ExitVerdict.GOOD
    assert "the stop did its job" in note


def test_exit_good_capture_boundary() -> None:
    assert exit_verdict(outcome(mfe="2.0", exit_r="1.40"))[0] is ExitVerdict.GOOD
    assert exit_verdict(outcome(mfe="2.0", exit_r="1.39"))[0] is ExitVerdict.OK


def test_exit_ignores_capture_over_a_trivial_denominator() -> None:
    """A trade offered +0.02R and stopped at -1R has a capture of -5000%.

    Same guard the renderer applies before printing a capture at all: below a
    quarter R the ratio has a meaningless denominator, and a LATE grade drawn
    from it would blame an exit for an entry that never worked.
    """
    grade, _ = exit_verdict(outcome(mfe="0.20", exit_r="-1.0"))
    assert grade is not ExitVerdict.LATE


def test_exit_unknown_when_nothing_is_measurable() -> None:
    grade, note = exit_verdict(outcome(mfe="0.0", exit_r="-1.0"))
    assert grade is ExitVerdict.UNKNOWN
    assert "ask again" in note


# ------------------------------------------------------------------ together


def test_good_buy_bad_exit_is_sayable() -> None:
    """The sentence the whole module exists to produce."""
    both = verdict(outcome(mfe="3.0", mae="-0.1", exit_r="0.2"))
    assert both is not None
    assert both.entry is EntryVerdict.GOOD
    assert both.exit is ExitVerdict.LATE


def test_bad_buy_good_exit_is_sayable() -> None:
    both = verdict(outcome(mfe="0.0", mae="-2.4", exit_r="-1.0", follow="-1.6"), rule="stop")
    assert both is not None
    assert both.entry is EntryVerdict.BAD
    assert both.exit is ExitVerdict.GOOD


def test_no_outcome_is_no_verdict() -> None:
    """None, not two shrugs. A trade that was never backfilled has not been
    measured, and a row of UNKNOWNs beside it reads as a measurement that came
    back empty rather than one that was never taken."""
    assert verdict(None) is None


def test_notes_never_judge_the_decision() -> None:
    """The standing rule: these describe a price path, they do not rule on a
    choice. A note that reaches for the vocabulary of blame has crossed from
    measurement into a claim that needs a population behind it."""
    banned = ("mistake", "should have", "shouldn't", "wrong call", "bad decision", "error")
    cases = [
        outcome(mfe=m, mae=a, exit_r=e, follow=f)
        for m, a, e, f in [
            ("3.0", "-0.1", "0.2", None),
            ("0.0", "-2.4", "-1.0", "-1.6"),
            ("2.0", "-0.9", "2.0", "1.5"),
            ("0.4", "-0.1", "0.3", None),
            ("1.2", "-0.6", "-0.5", None),
        ]
    ]
    for case in cases:
        both = verdict(case)
        assert both is not None
        text = (both.entry_note + " " + both.exit_note).lower()
        assert not any(word in text for word in banned), text

from trd.learn import GLOSSARY, all_entries, lookup
from trd.learn.glossary import Category, GlossaryEntry


def test_every_entry_complete() -> None:
    for entry in all_entries():
        assert entry.definition, f"{entry.key} missing definition"
        assert entry.term, f"{entry.key} missing term"
        assert entry.used_in, f"{entry.key} missing used_in"


def test_core_terms_present_with_formulas() -> None:
    for key in ("pl", "cost-basis", "fifo", "xirr", "cagr", "dca", "drift", "benchmark"):
        entry = GLOSSARY[key]
        assert entry.formula, f"{key} should show its formula"
        assert entry.example or entry.related, key


def test_indicator_entries_generated_from_registry() -> None:
    from trd.indicators import REGISTRY

    for key in REGISTRY:
        assert key in GLOSSARY
        assert GLOSSARY[key].category == Category.INDICATORS
        assert GLOSSARY[key].definition == REGISTRY[key].description


def test_lookup_exact_and_fuzzy() -> None:
    assert isinstance(lookup("xirr"), GlossaryEntry)
    assert isinstance(lookup("XIRR"), GlossaryEntry)
    assert isinstance(lookup("cost basis"), GlossaryEntry)  # space -> dash
    fuzzy = lookup("moving")
    assert isinstance(fuzzy, list)
    assert {e.key for e in fuzzy} >= {"sma", "ema"}
    assert lookup("zzzzz") == []


def test_related_keys_resolve() -> None:
    for entry in all_entries():
        for related in entry.related:
            assert related in GLOSSARY, f"{entry.key} -> dangling related '{related}'"


def test_every_number_the_engine_shows_can_be_explained() -> None:
    """`trd learn` promises every term trd shows. The engine reports R-multiples,
    expectancy and stops; before this it could explain none of them."""
    for key in (
        "r-multiple",
        "expectancy",
        "initial-stop",
        "trailing-stop",
        "profit-target",
        "position-sizing",
        "max-drawdown",
        "equity-curve",
        "earnings-blackout",
        "session-close",
        "survivorship",
        "lookahead",
    ):
        entry = GLOSSARY[key]
        assert entry.category == Category.ENGINE, key
        assert entry.example or entry.formula, f"{key} needs a worked example or a formula"


def test_engine_rule_entries_are_generated_from_the_registries() -> None:
    """Same treatment the indicators get: a rule's description lives in the rule,
    so the dictionary cannot drift from the code that runs."""
    from trd.engine import EXIT_RULES
    from trd.engine import REGISTRY as STRATEGIES

    for key, strategy in STRATEGIES.items():
        entry = GLOSSARY[key.replace("_", "-")]
        assert entry.category == Category.ENGINE
        assert entry.definition == strategy.description

    for rule in EXIT_RULES:
        entry = GLOSSARY[f"exit-{rule.key.replace('_', '-')}"]
        assert entry.category == Category.ENGINE
        assert entry.definition == rule.description

    # The order the rules actually run in is part of what a reader needs.
    assert "exit rule 1 of 6" in GLOSSARY["exit-stop"].term
    assert "exit rule 6 of 6" in GLOSSARY["exit-session-close"].term


def test_underscored_keys_resolve_as_trd_prints_them() -> None:
    """`trd engine report` prints `macd_cross`; looking up exactly what is on the
    screen has to work, so underscores normalise to hyphens."""
    for query in ("macd_cross", "macd-cross", "macd cross", "MACD_CROSS"):
        found = lookup(query)
        assert isinstance(found, GlossaryEntry), query
        assert found.key == "macd-cross"


def test_engine_entries_do_not_shadow_generic_words() -> None:
    """Exit keys are prefixed: a bare 'stop' or 'time' would be a poor dictionary
    entry and would collide with plainer language."""
    assert "stop" not in GLOSSARY
    assert "time" not in GLOSSARY
    assert "indicator" not in GLOSSARY

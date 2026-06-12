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

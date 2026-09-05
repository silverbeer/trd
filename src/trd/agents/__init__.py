"""Adapters that call a model.

Kept out of `services/` for the same reason Typer and Rich are: services stay
pure, testable and importable on a machine with no API key, no network and no
LLM SDK. Nothing here is imported by the trading path.

Deliberately empty of re-exports. `trd.agents.review_agent` imports pydantic-ai
at module scope, so importing it is what tells a caller the optional extra is
missing — and importing this package must not.
"""

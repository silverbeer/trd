from trd.engine import strategies  # noqa: F401 — importing populates the registry
from trd.engine.base import REGISTRY, Strategy, StrategySignal, register
from trd.engine.exits import DEFAULT_EXIT_PARAMS, ExitDecision, ExitRule
from trd.engine.exits import REGISTRY as EXIT_REGISTRY
from trd.engine.exits import RULES as EXIT_RULES
from trd.engine.exits import evaluate as evaluate_exits

__all__ = [
    "DEFAULT_EXIT_PARAMS",
    "EXIT_REGISTRY",
    "EXIT_RULES",
    "REGISTRY",
    "ExitDecision",
    "ExitRule",
    "Strategy",
    "StrategySignal",
    "evaluate_exits",
    "register",
]

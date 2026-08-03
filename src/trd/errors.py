class TrdError(Exception):
    """Base for all trd domain errors. The CLI catches these and renders them cleanly."""


class UnknownAccountError(TrdError):
    def __init__(self, name: str) -> None:
        super().__init__(f"No account named '{name}'. Run 'trd init' or check the name.")


class UnknownSymbolError(TrdError):
    def __init__(self, symbol: str) -> None:
        super().__init__(f"Could not resolve symbol '{symbol}' with the market data provider.")


class InsufficientPositionError(TrdError):
    def __init__(self, symbol: str, held: str, requested: str) -> None:
        super().__init__(f"Cannot sell {requested} {symbol}: only {held} held.")


class EnginePositionConflictError(TrdError):
    """A manual trade would desync the engine's book from the account's."""

    def __init__(self, symbol: str, account: str) -> None:
        super().__init__(
            f"{symbol} is held by the trading engine on '{account}'. A manual trade here "
            f"would leave engine_position and the account disagreeing: the engine would "
            f"still believe it holds the original quantity and would later sell all of it, "
            f"taking the account short. Let an exit rule close it, or use "
            f"'trd engine positions' to see what the engine is holding."
        )
        self.symbol = symbol
        self.account = account


class ProviderError(TrdError):
    """Market data provider failed (network, upstream change, unknown symbol)."""


class NotifyError(TrdError):
    """A notifier could not deliver. Never fatal — a scan that traded successfully
    must not fail because a chat message didn't send."""


class DatabaseBusyError(TrdError):
    def __init__(self) -> None:
        super().__init__(
            "Database is busy — another trd command is using it. "
            "Wait a moment and try again (DuckDB allows one writer at a time)."
        )

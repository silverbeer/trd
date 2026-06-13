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


class ProviderError(TrdError):
    """Market data provider failed (network, upstream change, unknown symbol)."""


class DatabaseBusyError(TrdError):
    def __init__(self) -> None:
        super().__init__(
            "Database is busy — another trd command is using it. "
            "Wait a moment and try again (DuckDB allows one writer at a time)."
        )

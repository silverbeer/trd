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


class NotTradableError(TrdError):
    """An instrument that cannot be held at all, not one that is merely a bad buy."""

    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"{symbol} is not a tradable instrument — a calculated number such as an "
            "index, not a holding, so there are no shares to own. trd tracks it for the "
            "market-regime gate, which reads it and never trades it. To take a position "
            "on what it measures you would need a product that follows it, which is a "
            "different symbol."
        )


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


class OpenIntentConflictError(EnginePositionConflictError):
    """A manual trade on a name a live engine has an order in flight for.

    A subclass so every caller that already refuses an engine-held symbol refuses
    this too: the shares are not on the book yet, but the fill that puts them
    there is already on its way.
    """

    def __init__(self, symbol: str, account: str) -> None:
        TrdError.__init__(
            self,
            f"{symbol} has an open order intent from the live engine on '{account}'. A "
            f"manual trade now would be booked beside the engine's own fill when it "
            f"arrives, and the engine's position would disagree with the account. Wait "
            f"for the order to resolve ('trd engine intents'), then trade.",
        )
        self.symbol = symbol
        self.account = account


class IntentAckError(TrdError):
    """An outbox ack trd refuses: the intent is already resolved, or the broker id
    offered disagrees with the one already recorded."""


class ProviderError(TrdError):
    """Market data provider failed (network, upstream change, unknown symbol)."""


class SymbolNotFoundError(ProviderError):
    """The provider has no such ticker — as opposed to being unable to answer.

    A subclass so every existing `except ProviderError` keeps catching it, while
    a caller that can act on the difference is able to. The two used to be
    distinguishable only by matching the message text, which is not a contract:
    an unknown ticker is a typo to correct, and a failed request is a reason to
    try later.
    """


class NotifyError(TrdError):
    """A notifier could not deliver. Never fatal — a scan that traded successfully
    must not fail because a chat message didn't send."""


class DatabaseBusyError(TrdError):
    def __init__(self) -> None:
        super().__init__(
            "Database is busy — another trd command is using it. "
            "Wait a moment and try again (DuckDB allows one writer at a time)."
        )

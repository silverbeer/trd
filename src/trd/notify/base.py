from typing import Protocol


class Notifier(Protocol):
    """Somewhere to send a short human-readable message.

    Same shape as MarketDataProvider: one narrow interface, swappable
    implementation, and a fake in the tests so nothing touches the network.
    """

    def send(self, text: str) -> None:
        """Deliver one message. Raises NotifyError on failure."""
        ...

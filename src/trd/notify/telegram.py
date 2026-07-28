import json
import os
import urllib.error
import urllib.request

from trd.errors import NotifyError

API_ROOT = "https://api.telegram.org"
TIMEOUT_SECONDS = 10


class TelegramNotifier:
    """Posts to a Telegram chat or channel via the Bot API.

    stdlib urllib on purpose — one POST of a few hundred bytes does not justify
    adding an HTTP dependency to a local-first tracker.
    """

    def __init__(self, token: str, chat_id: str, api_root: str = API_ROOT) -> None:
        self.token = token
        self.chat_id = chat_id
        self.api_root = api_root

    @property
    def url(self) -> str:
        return f"{self.api_root}/bot{self.token}/sendMessage"

    def send(self, text: str) -> None:
        # No parse_mode: signal reasons carry %, em-dashes and parentheses, and
        # Telegram's legacy Markdown rejects the whole message on an unmatched
        # character. A dropped alert is worse than a missing bold.
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
        ).encode()
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status >= 300:
                    raise NotifyError(f"Telegram returned HTTP {response.status}.")
        except urllib.error.HTTPError as exc:
            # The token is in the URL — never let it reach a log line.
            raise NotifyError(f"Telegram rejected the message (HTTP {exc.code}).") from None
        except urllib.error.URLError as exc:
            raise NotifyError(f"Could not reach Telegram: {exc.reason}") from None


def from_env(env: dict[str, str] | None = None) -> TelegramNotifier | None:
    """Build a notifier from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

    Returns None when unconfigured, so notification stays opt-in and a missing
    secret degrades to silence rather than an error.
    """
    source = env if env is not None else dict(os.environ)
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = source.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id)

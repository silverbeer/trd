import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable

from trd.errors import NotifyError

API_ROOT = "https://api.telegram.org"
TIMEOUT_SECONDS = 10

# What a live engine calls itself when nothing else is configured. Real money
# reads differently from paper at a glance, and the label is the first thing read.
LIVE_LABEL = "💵 live"


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

    def send(self, text: str, html: bool = False) -> None:
        # No parse_mode by default: signal reasons carry %, em-dashes and
        # parentheses, and Telegram's legacy Markdown rejects the whole message on
        # an unmatched character. A dropped alert is worse than a missing bold.
        # `html` is for a message built to escape every piece of data it carries
        # (the live report); nothing else opts in.
        body: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if html:
            body["parse_mode"] = "HTML"
        payload = json.dumps(body).encode()
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


def label_from_env(
    exit_params: dict[str, float] | None = None,
    env: dict[str, str] | None = None,
    live: bool = False,
) -> str:
    """Which engine a pushed fill came from.

    `TRD_ENGINE_LABEL` wins — the k3s deploy sets it per CronJob, so two engines
    label themselves with their own names. Unset, it falls back to what the rule
    set already says about the engine: a `flat_at_minute` is what makes a day
    engine, and everything else carries overnight. That fallback means the label
    is right with no configuration at all, which matters because the failure mode
    is silent — an unlabelled fill still looks like a perfectly good message.
    A live engine falls back to LIVE_LABEL before either: that it trades real
    money matters more than how long it holds.
    """
    source = env if env is not None else dict(os.environ)
    explicit = source.get("TRD_ENGINE_LABEL", "").strip()
    if explicit:
        return explicit
    if live:
        return LIVE_LABEL
    return "day" if (exit_params or {}).get("flat_at_minute", 0) > 0 else "swing"


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


# Why a live call to action was not sent. A message asking someone to approve a
# real-money order must reach that person, never the public feed by default.
ALERT_REFUSED = (
    "live alert NOT sent: neither TRD_LIVE_ALERT_CHAT_ID nor TRD_BOT_ALLOWED_USER_IDS "
    "is set, and a call to action on a real-money order is never posted to the "
    "public channel (TELEGRAM_CHAT_ID). Set one of them."
)


def alert_notifier_from_env(
    env: dict[str, str] | None = None,
    warn: Callable[[str], None] | None = None,
    channel_fallback: bool = False,
    error: Callable[[str], None] | None = None,
) -> TelegramNotifier | None:
    """A notifier for the live engine's ALERT chat: the messages that need a
    person to act — approve an order, look at a rejection, a halt.

    TRD_LIVE_ALERT_CHAT_ID, else the first id in TRD_BOT_ALLOWED_USER_IDS (a
    private chat with the bot has the user's own id). With neither, None — and
    `error` is told why: a live call to action does not fall back to the channel,
    where it would sit in a public feed among fills nobody has to act on.
    `channel_fallback` restores the old fallback to TELEGRAM_CHAT_ID (with `warn`
    told) for a caller whose messages are not calls to action on real money.
    Same bot token as `from_env`; None when there is none.
    """
    source = env if env is not None else dict(os.environ)
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    chat_id = source.get("TRD_LIVE_ALERT_CHAT_ID", "").strip()
    if not chat_id:
        allowed = source.get("TRD_BOT_ALLOWED_USER_IDS", "").replace(",", " ").split()
        chat_id = allowed[0] if allowed else ""
    if not chat_id:
        if not channel_fallback:
            if error is not None:
                error(ALERT_REFUSED)
            return None
        chat_id = source.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            return None
        if warn is not None:
            warn(
                "TRD_LIVE_ALERT_CHAT_ID and TRD_BOT_ALLOWED_USER_IDS are not set — live "
                "alerts go to TELEGRAM_CHAT_ID, the channel."
            )
    return TelegramNotifier(token, chat_id)

"""One Telegram API call, printing a human summary and never the token.

Split out of telegram.sh so the token arrives as an argv entry to a short-lived
process rather than being interpolated into a URL inside the shell, where it
would land in `set -x` output and in any trace the caller enabled.
"""

import json
import ssl
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org"


def _context() -> ssl.SSLContext:
    """Verify against certifi's bundle when it is importable.

    A python.org install on macOS ships an empty `cert.pem` until someone runs
    Install Certificates.command, so the default context rejects Telegram with
    "self-signed certificate in certificate chain" — a message that reads like
    interception and is really a missing trust store. certifi is already a
    transitive dependency here, and falling back to the default keeps this
    working anywhere the system store is sound.

    Deliberately never disables verification: this call carries a bot token.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def main() -> int:
    token, method, payload = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    request = urllib.request.Request(
        f"{API}/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=_context()) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Never exc.url — the token is in it.
        detail = exc.read().decode()[:200]
        print(f"{method} failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"{method} failed: {exc.reason}", file=sys.stderr)
        return 1

    result = body.get("result")
    if method == "getMe":
        print(f"ok — @{result.get('username')} (id {result.get('id')})")
    elif method == "sendMessage":
        print(f"sent to chat {result.get('chat', {}).get('id')}")
    elif method == "getUpdates":
        seen = {}
        for update in result or []:
            message = update.get("message") or {}
            sender = message.get("from") or {}
            if sender.get("id"):
                seen[sender["id"]] = (
                    sender.get("username") or sender.get("first_name") or "?",
                    (message.get("chat") or {}).get("id"),
                    (message.get("chat") or {}).get("type"),
                )
        if not seen:
            print("No messages waiting. Send one to the bot, then re-run.")
            print("(Telegram retains ~24h, and a running poller consumes them.)")
        for uid, (name, chat, kind) in seen.items():
            print(f"user_id={uid}  username={name}  chat_id={chat}  chat_type={kind}")
    else:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

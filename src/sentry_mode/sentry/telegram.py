"""One bounded Telegram request, run as a cancellable child with credentials on stdin."""

import http.client
import json
import sys

from sentry_mode.core.errors import HardwareError
from sentry_mode.sentry.config import TelegramAction, TelegramConfig


def send_message(payload: dict):
    connection = None
    try:
        config = TelegramConfig(bot_token=payload["token"], chat_id=payload["chat_id"])
        action = TelegramAction(text=payload["text"], silent=payload["disable_notification"])
        token = config.bot_token.get_secret_value()
        if not token or not config.chat_id:
            raise HardwareError("Configure a Telegram bot token and chat ID.")
        connection = http.client.HTTPSConnection("api.telegram.org", timeout=4)
        connection.request(
            "POST",
            f"/bot{token}/sendMessage",
            body=json.dumps(
                {
                    "chat_id": config.chat_id,
                    "text": action.text,
                    "disable_notification": action.silent,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            reason = {
                400: "Check the chat ID and message settings.",
                401: "Check the bot token.",
                403: "Start a chat with the bot or grant it access to the destination.",
                429: "Rate limited; increase the rule cooldown. No retry was made.",
            }.get(response.status, "Delivery failed or is unknown. No retry was made.")
            raise HardwareError(f"Telegram HTTP {response.status}: {reason}")
        result = json.loads(response.read(65537))
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HardwareError("Telegram rejected the message. Check bot access and chat ID.")
    except HardwareError:
        raise
    except Exception:
        # Network exception strings can contain the token-bearing request path.
        raise HardwareError(
            "Telegram request failed; delivery is unknown. No retry was made."
        ) from None
    finally:
        if connection is not None:
            connection.close()


def main():
    try:
        send_message(json.loads(sys.stdin.buffer.read(65537)))
    except Exception as exc:
        print(str(exc) if isinstance(exc, HardwareError) else "Invalid Telegram request.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Thin Telegram Bot API transport. No finance logic lives here.

Every method degrades to a falsy return rather than raising: a Telegram outage
must never break the transaction pipeline.
"""
import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org/bot"


def _post(url: str, body: bytes, timeout: int) -> bytes:
    """The single network primitive, isolated so tests can replace it."""
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class TelegramClient:
    def __init__(self, token: str, timeout: int = 20):
        self._token = (token or "").strip()
        self._timeout = timeout

    def _call(self, method: str, payload: dict):
        url = f'{API_ROOT}{self._token}/{method}'
        # A cron-driven bot always answers callbacks long after Telegram's
        # short validity window, so a failed answerCallbackQuery is normal,
        # not a fault - the answer itself was already delivered via
        # getUpdates. Computed once so both failure paths below agree.
        level = logging.DEBUG if method == 'answerCallbackQuery' else logging.WARNING
        try:
            raw = _post(url, json.dumps(payload).encode(), self._timeout)
            parsed = json.loads(raw.decode())
        except Exception as error:
            # Never log the raw error: some exceptions (e.g. http.client.InvalidURL)
            # embed the full request path - and therefore the bot token - in their
            # message, even though the URL itself is never interpolated directly.
            message = str(error)
            if self._token:
                message = message.replace(self._token, '***')
                # http.client.InvalidURL formats the offending path with %r, so a
                # token containing a control character, backslash or quote can
                # survive the literal replace above in its repr-escaped form.
                escaped = repr(self._token)[1:-1]
                if escaped != self._token:
                    message = message.replace(escaped, '***')
            logger.log(level, f'Telegram {method} failed: {error.__class__.__name__}: {message}')
            return None
        if not parsed.get("ok"):
            logger.log(level, f'Telegram {method} rejected: {parsed.get("description")}')
            return None
        return parsed.get("result")

    def get_updates(self, offset: int, poll_timeout: int = 0) -> list:
        """Fetch pending updates from the stored offset.

        `poll_timeout` is Telegram's long-poll: 0 returns immediately (what the
        cron path wants), anything higher holds the connection until an update
        arrives. Keep it below the client's own read timeout, or urlopen gives
        up before Telegram answers.

        The count is logged because it is the single most important input to
        the whole tagging loop and it used to be invisible: a run that fetched
        nothing and a run that fetched an answer and discarded it produced
        byte-identical logs, which made "my answer was not recorded"
        impossible to tell apart from "no answer had arrived yet".
        """
        result = self._call("getUpdates",
                            {"offset": offset, "timeout": poll_timeout})
        updates = result or []
        ids = [u.get("update_id") for u in updates]
        logger.info(f'getUpdates(offset={offset}) returned {len(updates)}: {ids}')
        return updates

    def get_webhook_info(self) -> dict:
        """Telegram's own view of the update queue. Reads; never consumes.

        `pending_update_count` is the watchdog's whole reason for existing: it
        says how many updates Telegram is holding, independently of what
        `getUpdates` hands us. The two disagreeing - Telegram holding updates
        while our poll comes back empty - is the exact signature of another
        consumer of the same bot token draining the queue first, which is
        otherwise completely silent and has cost the owner two days of answers.

        Deliberately NOT another `getUpdates` call: that one permanently
        forgets every update below its offset, so a diagnostic poll would
        destroy the very evidence it is looking for.
        """
        return self._call("getWebhookInfo", {}) or {}

    def send_message(self, chat_id, text: str, keyboard: list | None = None,
                     parse_mode: str | None = None):
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def edit_message(self, chat_id, message_id, text: str,
                     keyboard: list | None = None, parse_mode: str | None = None):
        """Rewrite a message already sent, dropping its keyboard by default.

        This is the only acknowledgement a cron-driven bot can actually give.
        `answerCallbackQuery` cannot: the run that applies a tap is never the
        run that sent the question, so by then Telegram has expired the
        callback id and the button spinner reports nothing at all.

        Omitting `reply_markup` leaves the old keyboard in place, so an empty
        `inline_keyboard` is sent explicitly to take the buttons away - a
        retired question must not still look answerable.
        """
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
                   "reply_markup": {"inline_keyboard": keyboard or []}}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("editMessageText", payload)

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})

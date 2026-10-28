"""Persistent state for the Telegram tagging loop.

Seven keys: the getUpdates offset, the chat id discovered from /start, a map of
tx_id -> unix timestamp for rows we've already asked about (so we don't spam
the same question every cron tick), a list of tx_ids the owner explicitly
skipped so they are never re-asked, `replies` (message_id -> tx_id, so a text
answer can be attributed to the exact row), `proposed` (tx_id -> a new label
awaiting confirmation) and `ask_count` (tx_id -> how many questions have gone
out, which is what stops the re-ask loop running forever) and
`undelivered_alert_at` (when the watchdog last reported that Telegram was
holding updates our poll never received). Corrupt or absent
state degrades to defaults rather than raising - losing the offset costs at
most one repeated question, which is far better than a crashed cron run.

Parsing is deliberately separate from storage. `parse_state`/`serialize_state`
own the validation; a *store* owns only where the JSON text lives. Both stores
therefore get byte-identical validation, and a new backend is two methods.

Two stores exist:

  FileStore  - `creds/telegram_state.json`. Correct on a machine with a durable
               disk (a laptop, a VPS).
  SheetStore - a cell in the spreadsheet. Required on a platform with an
               ephemeral filesystem, where every run starts from a fresh copy
               of the slug and anything written to disk is discarded. Losing
               `asked` there would re-ask every pending row on every run.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join(os.path.dirname(__file__), 'creds', 'telegram_state.json')


def _defaults() -> dict:
    # Built fresh every call: "asked"/"declined" are mutable containers, and a
    # module-level dict literal would let separate callers share (and corrupt)
    # each other's state.
    return {"offset": 0, "chat_id": None, "asked": {}, "declined": [],
            "replies": {}, "proposed": {}, "ask_count": {},
            "undelivered_alert_at": 0}


class FileStore:
    """Telegram state in a local JSON file."""

    def __init__(self, path: str = STATE_PATH):
        self.path = path

    def read(self) -> str | None:
        # OSError covers missing files and permission problems (a missing file
        # is the normal first-run case, not an error); ValueError covers the
        # UnicodeDecodeError raised when the file is binary.
        try:
            with open(self.path) as fh:
                return fh.read()
        except (OSError, ValueError) as error:
            logger.info(f'No Telegram state file ({error.__class__.__name__}); using defaults')
            return None

    def write(self, text: str) -> None:
        with open(self.path, 'w') as fh:
            fh.write(text)


class SheetStore:
    """Telegram state in the spreadsheet, for hosts with no durable disk.

    Reads fall back to `legacy_path` once: on the first run after a move to
    sheet-backed state the cell is still empty, and without the fallback the
    owner's `declined` ledger would be silently discarded and every declined
    row asked about again. The fallback stops mattering as soon as the first
    write lands, and on a fresh host the file simply isn't there.
    """

    def __init__(self, sheets, legacy_path: str = STATE_PATH):
        self._sheets = sheets
        self._legacy_path = legacy_path

    def read(self) -> str | None:
        text = self._sheets.read_state()
        if text:
            return text
        legacy = FileStore(self._legacy_path).read()
        if legacy:
            logger.info('Sheet state is empty; seeding from the legacy local file')
        return legacy

    def write(self, text: str) -> None:
        self._sheets.write_state(text)


def parse_state(text: str | None) -> dict:
    """Validate stored JSON into the state dict.

    Never raises: every malformed shape degrades to the default for that key.
    """
    if text is None:
        return _defaults()

    # ValueError covers both json.JSONDecodeError and UnicodeDecodeError.
    try:
        raw = json.loads(text)
    except ValueError as error:
        logger.info(f'Telegram state is not valid JSON ({error.__class__.__name__}); using defaults')
        return _defaults()

    # Valid JSON that is not an object (42, null, "text", []) would break the key
    # copy below, so reject it here rather than raising out of a cron job.
    if not isinstance(raw, dict):
        logger.info('Telegram state did not contain a JSON object; using defaults')
        return _defaults()

    state = _defaults()

    if "offset" in raw:
        try:
            state["offset"] = int(raw["offset"])
        except (TypeError, ValueError):
            logger.warning('Telegram state "offset" is not an integer; using default')

    if "chat_id" in raw:
        state["chat_id"] = raw["chat_id"]

    if "asked" in raw:
        if isinstance(raw["asked"], dict):
            cleaned = {}
            dropped = 0
            for tx_id, ts in raw["asked"].items():
                if isinstance(tx_id, str) and isinstance(ts, (int, float)):
                    cleaned[tx_id] = ts
                else:
                    dropped += 1
            if dropped:
                logger.warning(f'Dropped {dropped} invalid "asked" entries '
                               f'(bad key/value type); keeping the rest')
            state["asked"] = cleaned
        else:
            logger.warning('Telegram state "asked" is not an object; using default')

    if "declined" in raw:
        if isinstance(raw["declined"], list):
            cleaned = [tx_id for tx_id in raw["declined"] if isinstance(tx_id, str)]
            dropped = len(raw["declined"]) - len(cleaned)
            if dropped:
                logger.warning(f'Dropped {dropped} invalid "declined" entries '
                               f'(not a string); keeping the rest')
            state["declined"] = cleaned
        else:
            logger.warning('Telegram state "declined" is not a list; using default')

    # tx_id -> how many times we have asked about it. Bounds the re-ask loop:
    # without it a row nobody answers is re-asked twice a day forever, which is
    # what drove the owner to press "Інше" on 34 rows just to stop the noise.
    if "ask_count" in raw:
        if isinstance(raw["ask_count"], dict):
            state["ask_count"] = {
                str(tx): int(n) for tx, n in raw["ask_count"].items()
                if isinstance(n, (int, float)) and not isinstance(n, bool)}
        else:
            logger.warning('Telegram state "ask_count" is not an object; using default')

    # When the watchdog last told the owner that Telegram was holding updates
    # our poll never saw. Purely a cooldown: the anomaly persists for as long
    # as whoever is eating the updates keeps doing it, and a message every ten
    # minutes would be noise rather than news.
    if "undelivered_alert_at" in raw:
        value = raw["undelivered_alert_at"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            state["undelivered_alert_at"] = value
        else:
            logger.warning('Telegram state "undelivered_alert_at" is not a '
                           'number; using default')

    # message_id -> tx_id, and tx_id -> proposed label. Both are plain string
    # maps; anything else degrades to empty rather than raising in a cron job.
    for key in ("replies", "proposed"):
        if key not in raw:
            continue
        if isinstance(raw[key], dict):
            state[key] = {str(k): str(v) for k, v in raw[key].items()}
        else:
            logger.warning(f'Telegram state "{key}" is not an object; using default')

    return state


def serialize_state(state: dict) -> str:
    """The persisted subset of the state, as JSON.

    Indexes with [] rather than .get(): a caller that forgot a ledger must fail
    loudly rather than quietly erase it. That was the reason this previously
    took every value positionally with no defaults; six positional arguments is
    past where that reads well, but the property is the same.

    Message ids are coerced to strings because JSON object keys are strings - an
    int key would round-trip to a string and never match again.
    """
    return json.dumps({
        "offset": state["offset"],
        "chat_id": state["chat_id"],
        "asked": dict(state["asked"]),
        "declined": list(state["declined"]),
        "replies": {str(k): str(v) for k, v in state["replies"].items()},
        "proposed": {str(k): str(v) for k, v in state["proposed"].items()},
        "ask_count": {str(k): int(v) for k, v in state["ask_count"].items()},
        "undelivered_alert_at": state["undelivered_alert_at"],
    }, ensure_ascii=False)


def load_state(path: str = STATE_PATH) -> dict:
    """Read state from a file path. Convenience wrapper over FileStore."""
    return parse_state(FileStore(path).read())


def save_state(path: str, state: dict) -> None:
    """Write state to a file path. Convenience wrapper over FileStore."""
    FileStore(path).write(serialize_state(state))

"""Long-polling worker: the same pipeline, but answers land in seconds.

**This is the production entry point.** It replaced the ten-minute Scheduler
poll, which lost the owner's answers twice in four days for two different
reasons that were both really the same reason: a cron that asks Telegram once
every ten minutes and remembers its place in a cell is racing three separate
rules it cannot see.

  - Updates expire after 24 hours. An offset that has not moved for a day
    arrives to find the queue already emptied (2026-08-11).
  - `getUpdates(offset=N)` **permanently forgets** every update below N. There
    is no error and no second chance.
  - Telegram allows exactly one consumer per bot token. A second one receives
    an empty batch with `ok: true` and nothing logged anywhere. On 2026-08-14
    the owner's taps reached Telegram (`pending_update_count` went 0 -> 1) and
    vanished about three minutes later without any run of ours reading them.

Holding the connection open answers all three: an update is taken within
milliseconds of existing, so it never ages out, the offset advances
continuously, and a competitor polling on a timer loses every race it is in.

**It must be the only getUpdates consumer while it runs.** Set the
`WORKER_ACTIVE` config var and `main.main()` stands down, so a Scheduler job
left in place cannot steal updates from under this process. Deleting that job
is still the tidy end state; the guard is what makes forgetting harmless.

The loop deliberately does NOT re-implement any logic. It long-polls purely to
find out *whether* something arrived, then hands off to `run_tagging`, which
re-fetches from the same unadvanced offset and applies the answer through
exactly the code the cron path used. It calls `pipeline._run` rather than
`pipeline.main`, and so is not itself blocked by the `WORKER_ACTIVE` guard.
"""
import logging
import os
import time
from datetime import datetime

import main as pipeline
from auth_service import AuthService
from spread_sheet_service import SpreadSheetService
from telegram_client import TelegramClient
from telegram_service import run_tagging

logger = logging.getLogger(__name__)

# Telegram holds the connection this long when there is nothing to say. The
# client's read timeout must exceed it or urlopen gives up first.
POLL_SECONDS = 25
CLIENT_TIMEOUT = POLL_SECONDS + 15

# How often to run the ordinary pipeline (mail, staleness, digest) from inside
# the loop, matching what the scheduler used to do.
PIPELINE_EVERY = 600

# A failing poll must not become a hot loop against the API.
BACKOFF_SECONDS = 15

# Startup backoff. Building the Google clients reads the sheet, so a transient
# Sheets 429 - the quota is 60 reads a minute and is easy to brush against -
# used to kill the process before the loop ever began. For a Scheduler run that
# cost ten minutes; for a resident dyno it hands control to Heroku's restart
# backoff, which doubles up to twenty. Retrying here instead keeps the outage
# and the recovery the same length.
STARTUP_BACKOFF_SECONDS = 10
STARTUP_BACKOFF_MAX_SECONDS = 120


class FatalStartupError(Exception):
    """A startup failure retrying cannot fix, such as a missing credential.

    Distinguished from a transient one on purpose: a dyno that stays up
    retrying forever against a config mistake looks healthy on the dashboard
    while never doing any work, which is a worse failure than crashing.
    """


def connect_with_retry(build, backoff: int = STARTUP_BACKOFF_SECONDS):
    """Call `build` until it succeeds, pacing retries. Returns its result.

    Deliberately without an attempt limit. Giving up means exiting, and
    exiting is precisely what makes a brief outage expensive here.
    """
    attempt = 0
    while True:
        try:
            return build()
        except FatalStartupError:
            raise
        except Exception as error:
            attempt += 1
            wait = min(backoff * 2 ** (attempt - 1), STARTUP_BACKOFF_MAX_SECONDS)
            logger.error(f'Startup attempt {attempt} failed '
                         f'({error.__class__.__name__}); retrying in {wait}s')
            time.sleep(wait)


def _pipeline_pass(token, alert_chat_id) -> None:
    """One ordinary run. Never raises: the loop has to survive a bad minute."""
    try:
        pipeline._run(token, alert_chat_id)
    except Exception as error:
        logger.error(f'Pipeline pass failed: {error}')


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.getenv('TELEGRAM_API', '')
    alert_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token:
        raise SystemExit('TELEGRAM_API is not set; nothing to poll')

    def _build():
        auth = AuthService()
        return SpreadSheetService(auth.service_sheets, auth.spreadsheet_id)

    # Both of those reach Google, and AuthService smoke-tests each client, so
    # this is where a transient quota error lands.
    sheets = connect_with_retry(_build)
    client = TelegramClient(token, timeout=CLIENT_TIMEOUT)

    from telegram_state import SheetStore, parse_state
    store = SheetStore(sheets)

    logger.info(f'Worker up. Long-polling every {POLL_SECONDS}s, '
                f'pipeline every {PIPELINE_EVERY}s.')
    last_pipeline = 0.0

    while True:
        now = time.time()
        if now - last_pipeline >= PIPELINE_EVERY:
            if pipeline.within_active_hours(datetime.now()):
                logger.info('--- pipeline pass ---')
                _pipeline_pass(token, alert_chat_id)
            else:
                logger.info('Outside active hours; skipping the pipeline pass '
                            '(Telegram polling continues)')
            last_pipeline = now

        # Peek only. The offset is NOT advanced here, so whatever arrives is
        # still unconfirmed and run_tagging re-fetches and applies it through
        # the production path.
        offset = parse_state(store.read()).get('offset', 0)
        try:
            updates = client.get_updates(offset, poll_timeout=POLL_SECONDS)
        except Exception as error:
            logger.error(f'Poll failed: {error}; backing off')
            time.sleep(BACKOFF_SECONDS)
            continue

        if not updates:
            continue

        logger.info(f'>>> {len(updates)} update(s) arrived; handing to run_tagging')
        run_tagging(sheets, token)
        after = parse_state(store.read()).get('offset', 0)
        logger.info(f'<<< offset {offset} -> {after}')


if __name__ == '__main__':
    main()

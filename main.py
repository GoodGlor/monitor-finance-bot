import os
import logging
from datetime import datetime

import digest
from auth_service import AuthService
from gmail_service import GmailService
from spread_sheet_service import SpreadSheetService
from telegram_service import (
    is_stale, notify_failure, notify_stale, run_tagging, should_alert_daily,
)

logger = logging.getLogger(__name__)

# Quiet hours. The scheduler cannot express a time window - it only offers
# fixed intervals - so the window is enforced here instead. Note this does not
# stop the dyno starting; it only makes it exit before doing any API work.
# Local time, which is why TZ must be set on the host.
# 09:00, not 05:00. The tagging loop re-asks its whole backlog as soon as the
# cooldown lapses, so the first run of the day delivers a burst of questions -
# and at 05:00 that burst was a phone full of notifications before the owner
# was awake. Nothing here needs to happen before morning: mail that arrives
# overnight is still UNREAD and is picked up by the first run.
ACTIVE_START_HOUR = int(os.getenv('ACTIVE_START_HOUR', '9'))
ACTIVE_END_HOUR = int(os.getenv('ACTIVE_END_HOUR', '23'))

# The digest rides this run rather than a second Scheduler job: Heroku's
# Scheduler has no CLI for jobs, so that job is a manual dashboard step. Kept
# as its own knob even though it now equals ACTIVE_START_HOUR: the two answer
# different questions, and moving the working day earlier again must not
# silently drag the spending report back to dawn with it.
DIGEST_HOUR = int(os.getenv('DIGEST_HOUR', '9'))


def within_active_hours(now=None) -> bool:
    """Whether the pipeline should do anything at this local hour.

    End is exclusive: 5..23 means the last run of the day starts at 22:50.
    """
    now = now or datetime.now()
    return ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR


def main():
    logging.basicConfig(level=logging.INFO)
    # Read before anything can fail: an auth failure must still be reportable,
    # and the chat id cannot come from the sheet when the sheet is what broke.
    token = os.getenv('TELEGRAM_API', '')
    alert_chat_id = os.getenv('TELEGRAM_CHAT_ID')

    # Telegram allows exactly one consumer per bot token, and enforces it in
    # the worst possible way: the loser of the race gets an empty batch with
    # `ok: true` and no error anywhere, so an answer the owner really did send
    # simply evaporates. When the long-polling worker is up it owns the bot,
    # and this scheduled run must stand down entirely rather than take turns
    # stealing its updates. Read here rather than at import so the guard is a
    # config var the owner can flip without a deploy.
    if os.getenv('WORKER_ACTIVE'):
        logger.info('WORKER_ACTIVE is set; the worker dyno owns this run.')
        return

    if not within_active_hours():
        logger.info(f'Outside active hours '
                    f'({ACTIVE_START_HOUR}:00-{ACTIVE_END_HOUR}:00); nothing to do.')
        return

    try:
        _run(token, alert_chat_id)
    except Exception as error:
        # Report, then re-raise: the scheduler should still record a failure.
        logger.error(f'Run failed: {error}')
        notify_failure(token, alert_chat_id, error)
        raise


def _run(token, alert_chat_id):
    auth = AuthService()
    gmail = GmailService(auth.service_gmail)
    spread_sheet = SpreadSheetService(auth.service_sheets, auth.spreadsheet_id)

    # Apply Sheet2 category-override rules during parsing
    gmail.rules = spread_sheet.rules

    mails = gmail.process_all_unread(spread_sheet.existing_ids)
    sheets_rows = mails.get('sheets_rows', [])

    if not sheets_rows:
        logger.info("No valid transaction rows to process.")
    else:
        try:
            # ATOMIC OPERATION: Append and get confirmed IDs
            successfully_appended_ids = spread_sheet.append_data(sheets_rows)

            if successfully_appended_ids:
                # ONLY mark as read the exact emails that were saved to DB
                gmail.mark_as_read(successfully_appended_ids)

        except Exception as e:
            # STRICT FALLBACK: If Google Sheets API fails, emails remain UNREAD
            logger.error(f"Critical pipeline failure during DB write: {e}")
            # Swallowed on purpose (mail stays UNREAD and is retried), so it
            # would otherwise never surface anywhere the owner looks.
            notify_failure(token, alert_chat_id, e)

    # Runs last and UNCONDITIONALLY: rows are already committed, so a Telegram
    # failure costs nothing, and pending rows still need answering on runs that
    # brought no new mail.
    run_tagging(spread_sheet, token)

    _check_staleness(spread_sheet, token, alert_chat_id)

    _maybe_digest(spread_sheet, token, alert_chat_id, datetime.now())


def _maybe_digest(spread_sheet, token, chat_id, now) -> None:
    """Send the weekly/monthly report if today owes one.

    Wrapped end to end for the same reason run_tagging is: by the time this
    runs the pipeline has committed its rows and marked mail read, and a
    Telegram or OpenRouter hiccup must not turn a successful run into a visible
    failure. digest.run is itself a no-op on days that owe nothing, costing one
    state read.
    """
    if now.hour < DIGEST_HOUR:
        return
    try:
        digest.run(spread_sheet, token, chat_id, today=now.date())
    except Exception as error:
        logger.error(f'Digest failed, pipeline unaffected: {error}')


def _check_staleness(spread_sheet, token, alert_chat_id):
    """Warn once a day if no transaction has arrived in a long time.

    A run that succeeds but finds no mail looks exactly like a healthy quiet
    period, so without this a silently-broken feed (PrivatBank stopped sending,
    a Gmail filter started catching the notifications) would never surface.

    Checked only in the daily window so the Sheets read costs nothing on the
    other 100-odd runs a day.
    """
    now = datetime.now()
    if not should_alert_daily(now):
        return
    latest = spread_sheet.latest_transaction_at()
    if is_stale(latest, now):
        logger.warning(f'No new transactions since {latest}')
        notify_stale(token, alert_chat_id, latest, now)


if __name__ == '__main__':
    main()

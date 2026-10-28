"""The Telegram tagging loop: ask about unresolved rows, apply the answers."""
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import digest
import openrouter_client
from categories import (
    CANON, CARD_TAIL, EXPENSE, OWN_CARDS, UNKNOWN, apply_override, match_text,
)
from telegram_client import TelegramClient
from telegram_state import FileStore, SheetStore, parse_state, serialize_state

logger = logging.getLogger(__name__)

# Telegram caps callback_data at 64 bytes; a Gmail message id is 16
# characters, leaving the rest of the budget for `{tx_id}:{label}`.
CALLBACK_MAX_BYTES = 64

# One run's worth of questions. The queue drains over the following runs rather
# than arriving as a wall of messages.
MAX_QUESTIONS_PER_RUN = 8

# A movement already carries a category - the bank said it was a transfer, a
# withdrawal, a credit. What it does not carry is a purpose, and the purpose is
# a different question depending on which way the money went.
QUESTION_BY_KIND = {
    "transfer": 'Переказ. За що це було?',
    "cash": 'Зняття готівки. На що пішли ці гроші?',
    "incoming": 'Надходження. Звідки ці гроші?',
    "own_card": ('Переказ на свою картку. Це погашення кредиту '
                 'чи рух між своїми картками?'),
    "requisites": 'Платіж за реквізитами. За що це було?',
}

# Analytics drops a row by its Type, never its category, so an own-card
# transfer the owner calls a credit repayment has to stop being INTERNAL or it
# stays as invisible as it was.
CREDIT = "Кредит"

# Cannot equal any real category, so "did a rule fire?" is an identity check
# rather than a comparison a rule returning the same category answers wrongly.
_NO_RULE = object()

# An own-card question has two real answers and offering twenty-one buttons
# would bury both. The list is closed on purpose: any other purpose for money
# moved between one's own cards is rare enough to type through `Інше`.
OWN_CARD_CHOICES = [CREDIT, OWN_CARDS]


def choices_for(row: dict, choices: list) -> list:
    """The labels to offer for this row - and therefore to accept from it.

    One function for both sides on purpose. When the offered list and the
    accepted list are computed separately they drift, and the symptom is a
    button the bot drew itself being rejected the moment it is tapped.
    """
    if (row or {}).get("kind") == "own_card":
        return list(OWN_CARD_CHOICES)
    return choices


def _cooldown_key(row: dict):
    """What "we already asked about this" means for a row.

    The counterparty, not the row: one answer closes every queued row that
    shares a card or a payee, so both the per-run dedupe and the cooldown have
    to group by the same thing or they undo each other. A row whose recipient
    the bank never names has no reusable key and falls back to its own id.
    """
    return (row.get("kind"), _learn_key(row) or row.get("tx_id"))


def siblings_for(row: dict, label: str, pending_by_id: dict) -> list:
    """The other queued rows this answer's rule is about to silence.

    Defined as exactly that set, and computed by asking `apply_override` -
    rather than by re-deriving "same card" or "same payee" - so the two can
    never disagree. Anything the rule covers but the answer skips would keep
    its old label and never be asked about again, silently: three transfers to
    *1111, five payments to one ФОП.

    Same kind only. A payee rule may well cover an unresolved row too, but
    turning that row's category over without asking is a different decision
    from the one the owner just made.
    """
    key = _learn_key(row)
    if not key:
        return []
    rule = {key: label}
    return [other for other in pending_by_id.values()
            if other.get("kind") == row.get("kind")
            and other.get("tx_id") != row.get("tx_id")
            and apply_override(_NO_RULE, other.get("merchant", ""),
                               rule) is not _NO_RULE]


def _card_tail(text: str) -> str:
    """The `*NNNN` the bank writes for a card, or "" when there is none."""
    match = CARD_TAIL.search(text or "")
    return match.group(0).replace(" ", "") if match else ""


TX_ID_LENGTH = 16

# Display-order pool only. NOT an index space: sorting survives display
# reordering but not membership changes (a renamed/added/removed label shifts
# every index after it), so callback_data must carry the label itself, never
# an index into this list. Falls back to this constant if no choices are
# offered in the sheet; validation of incoming answers uses the choices parameter.
CATEGORY_CHOICES = sorted(set(CANON.values()))

# "Інше" means "none of these fit, I will type it" - it opens the free-text
# path and deliberately does NOT silence the row. Skipping is a separate,
# explicitly-labelled button: conflating the two made every "let me type it"
# tap kill the row forever, which is how 34 of 35 pending rows ended up
# permanently unanswerable with nothing on screen to say so.
OTHER_LABEL = "Інше"
SKIP_LABEL = "Пропустити"

# Questions per row before it goes quiet. A row nobody answers used to be
# re-asked every ASK_COOLDOWN_HOURS forever; three identical live questions in
# the chat is exactly what "I answer the same question three times" was.
MAX_ASKS_PER_ROW = 3

# Merchants that carry no reusable key - the bank never states the recipient.
UNLEARNABLE = {"на картку"}

SEND_PAUSE_SECONDS = 0.5
ASK_COOLDOWN_HOURS = 12
_ASK_COOLDOWN_SECONDS = ASK_COOLDOWN_HOURS * 3600


def encode_callback(tx_id: str, label: str) -> str:
    return f'{tx_id}:{label}'


def decode_callback(data: str):
    # partition (not rpartition) on the FIRST colon: labels never contain a
    # colon, but splitting from the right would silently truncate a label if
    # one ever did.
    tx_id, sep, label = (data or "").partition(':')
    if not tx_id or not label:
        return None
    return tx_id, label


def sanitize_choices(labels: list) -> list:
    """Drop labels the bot cannot safely offer.

    The owner edits these by hand in the sheet, so they can be too long or
    collide with a sentinel. A label that survives here is guaranteed encodable.
    """
    safe = []
    for label in labels:
        if label in (OTHER_LABEL, SKIP_LABEL, UNKNOWN):
            logger.warning(f'Category {label!r} is reserved; not offered as a button')
            continue
        if len(encode_callback('0' * TX_ID_LENGTH, label).encode()) > CALLBACK_MAX_BYTES:
            logger.warning(
                f'Category {label!r} is too long for a Telegram button and was skipped')
            continue
        safe.append(label)
    return safe


def is_learnable(merchant: str) -> bool:
    """Reject merchant text too generic/unstable to become a substring rule.

    `categories.apply_override` matches rule keys as a case-insensitive
    substring against every future merchant, and additionally treats any key
    with 4+ digits as a card-last-4 wildcard. So the key must be long enough
    and textual enough not to accidentally match unrelated transactions.
    """
    text = (merchant or "").strip()
    if not text or text.lower() in UNLEARNABLE:
        return False
    if len(text) < 4:
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    if sum(ch.isdigit() for ch in text) >= 4:
        return False
    return True


# Telegram divides a row's width evenly between its buttons and truncates any
# label that does not fit, so the readable length per button is roughly
# ROW_BUDGET / (buttons in that row). Fixed 3-per-row rendered "Переказ рі…"
# and "Переказ др…" - two different categories, indistinguishable on screen,
# one tap apart. Measured from a live screenshot: at three per row about
# eleven characters survive, so the budget is a little under that times three.
ROW_BUDGET = 32
MAX_PER_ROW = 3


def _pack(labels: list) -> list:
    """Group labels into rows so none of them gets truncated.

    Order is never changed - it is the owner's curated Категорії order - only
    where the rows break. A label too long to share a row gets one to itself
    rather than being cut.
    """
    rows: list = []
    for label in labels:
        row = rows[-1] if rows else None
        if row is not None and len(row) < MAX_PER_ROW:
            widest = max(len(item) for item in row + [label])
            if widest * (len(row) + 1) <= ROW_BUDGET:
                row.append(label)
                continue
        rows.append([label])
    return rows


def build_keyboard(tx_id: str, choices: list | None = None) -> list:
    """Render one button per label, in the order given.

    The order is deliberate and comes from the owner's Категорії tab, so it is
    never re-sorted here. Frequency ordering applies only to the built-in
    fallback list, and is applied by the caller before this point.
    """
    choices = choices or CATEGORY_CHOICES
    rows = [[{"text": c, "callback_data": encode_callback(tx_id, c)}
             for c in row]
            for row in _pack(choices)]
    # Two distinct escapes on the last row, because they mean opposite things:
    # "Інше" keeps the row in the queue and invites a typed answer, "Пропустити"
    # retires it. One button doing both is what silenced 34 rows.
    rows.append([{"text": OTHER_LABEL,
                  "callback_data": encode_callback(tx_id, OTHER_LABEL)},
                 {"text": SKIP_LABEL,
                  "callback_data": encode_callback(tx_id, SKIP_LABEL)}])
    return rows


CONFIRM_LIST_LIMIT = 10

# Only report a failure during the first minutes of an hour. See should_alert.
ALERT_WINDOW_MINUTES = 10
# Exception text can be long and carries nothing useful past the first line.
ALERT_MAX_CHARS = 400


def should_alert(now=None) -> bool:
    """Stateless throttle for failure alerts.

    A failing run usually cannot record that it alerted: the thing that broke
    is normally the Google auth or Sheets path that holds our state. Gating on
    the clock needs no storage and gives the behaviour an unattended job wants
    - a persistent outage reports about once an hour, while a transient blip
    that clears within the hour stays silent instead of waking the owner.
    """
    now = now or datetime.now(timezone.utc)
    return now.minute < ALERT_WINDOW_MINUTES


def notify_failure(token: str, chat_id, error, now=None) -> bool:
    """Best-effort "this run broke" message. Returns whether one was sent.

    Never raises: this runs inside an exception handler, and a failure to
    report a failure must not replace the original error.
    """
    if not token or not chat_id:
        logger.warning('No Telegram alert target (TELEGRAM_CHAT_ID unset); '
                       'failure not reported')
        return False
    if not should_alert(now):
        logger.info('Failure alert suppressed by the hourly throttle')
        return False
    text = f'{type(error).__name__}: {error}'[:ALERT_MAX_CHARS]
    try:
        TelegramClient(token).send_message(chat_id, f'❌ Monitor Finance\n{text}')
        return True
    except Exception as send_error:
        logger.error(f'Could not send the failure alert: {send_error}')
        return False


# A quiet feed is the failure this catches: a run that succeeds but finds no
# mail is indistinguishable from a healthy quiet period, so nothing else would
# ever notice that PrivatBank stopped sending or a Gmail filter started
# swallowing the notifications.
STALE_AFTER_HOURS = 48
# Local hour at which the daily staleness reminder goes out. Must fall inside
# the active window in main.py, or the run that would send it never happens.
STALE_ALERT_HOUR = 10


def is_stale(latest, now, hours: int = STALE_AFTER_HOURS) -> bool:
    """Whether the newest transaction is older than the threshold.

    `latest` of None means the lookup failed or the sheet is empty; that is
    reported as not-stale rather than crying wolf about a read error.
    """
    if latest is None:
        return False
    return (now - latest) > timedelta(hours=hours)


def should_alert_daily(now) -> bool:
    # Once a day, not once an hour: staleness persists until the owner acts on
    # it, and an hourly reminder for a two-day-old feed is nagging, not news.
    return now.hour == STALE_ALERT_HOUR and now.minute < ALERT_WINDOW_MINUTES


def notify_stale(token: str, chat_id, latest, now) -> bool:
    """Best-effort "nothing has arrived in a while" message. Never raises."""
    if not token or not chat_id:
        return False
    hours = int((now - latest).total_seconds() // 3600)
    text = (f'⚠️ Monitor Finance\n'
            f'Немає нових транзакцій уже {hours} год.\n'
            f'Остання: {latest:%Y-%m-%d %H:%M}\n'
            f'Перевірте пошту від info@pb.ua та фільтри Gmail.')
    try:
        TelegramClient(token).send_message(chat_id, text)
        return True
    except Exception as error:
        logger.error(f'Could not send the staleness alert: {error}')
        return False


# How long the watchdog stays quiet after reporting eaten answers. The
# condition persists for as long as whatever is draining the queue keeps
# draining it, so without a cooldown the owner gets a message every run.
UNDELIVERED_ALERT_COOLDOWN_HOURS = 6


def check_undelivered(client, state: dict, received: int, now: float) -> str:
    """Report answers Telegram accepted but never handed to us. "" if fine.

    The silent failure this exists for, seen twice: the owner taps, Telegram
    queues the update, and it disappears from the queue minutes later without
    any run of ours reading it - another consumer of the same bot token got
    there first. Every poll of ours logs a perfectly healthy `returned 0: []`,
    the sheet never changes, and nothing anywhere says a word. Both times the
    owner found it by noticing, days apart.

    `pending_update_count` is the one observable that separates "the owner has
    not answered yet" from "the owner answered and we lost it": Telegram
    holding updates while our own poll came back empty cannot be explained by
    an idle owner. Checked only when a question is actually outstanding, so a
    stranger messaging the bot never pages anyone, and skipped entirely when
    the poll did return something - an update landing between our getUpdates
    and this call is an ordinary race, not the fault.
    """
    if received:
        return ""
    if not state.get("asked"):
        return ""

    pending = client.get_webhook_info().get("pending_update_count") or 0
    if pending <= 0:
        return ""

    # `or 0` then a truthiness test, not a bare subtraction: 0 is the default
    # and means "never reported", which must always fire rather than being
    # measured as a gap from the epoch.
    last = state.get("undelivered_alert_at", 0) or 0
    if last and now - last < UNDELIVERED_ALERT_COOLDOWN_HOURS * 3600:
        logger.warning(f'Telegram is holding {pending} update(s) we did not '
                       f'receive; alert suppressed by the cooldown')
        return ""
    state["undelivered_alert_at"] = now
    logger.error(f'Telegram is holding {pending} update(s) that our poll did '
                 f'not receive - another consumer of this bot token?')
    return ('⚠️ Monitor Finance\n'
            f'Telegram тримає {pending} відповідь(ей), які до бота не дійшли.\n'
            'Схоже, того самого бота читає ще хтось і забирає їх собі.\n'
            'Ваші натискання зараз можуть не записуватись.')


def format_confirmation(applied: list) -> str:
    """Summarise the answers applied this run.

    Exists because the run that applies a tap is not the run that sent the
    question: by then Telegram's callback id has expired, `answerCallbackQuery`
    fails, and the button gives the owner no feedback at all. This message is
    the acknowledgement the spinner cannot provide.
    """
    lines = [f'✅ Категоризовано: {len(applied)}']
    for item in applied[:CONFIRM_LIST_LIMIT]:
        merchant = (item.get("merchant") or "").strip() or "—"
        lines.append(f'• {merchant} → {item.get("label", "")}')
    remaining = len(applied) - CONFIRM_LIST_LIMIT
    if remaining > 0:
        lines.append(f'…та ще {remaining}')
    return "\n".join(lines)


def format_question(row: dict) -> str:
    # A transfer already carries a category - the bank said it was a transfer.
    # What it does not carry is a purpose, so the question is a different one.
    prompt = QUESTION_BY_KIND.get(row.get("kind"), 'Оберіть категорію:')
    return (f'{row.get("date", "")} {row.get("time", "")}  '
            f'{row.get("amount", "")}₴\n'
            f'{row.get("merchant", "")}\n'
            f'{prompt}')


def _messages_for(replies: dict, tx_id: str) -> list:
    """Every question message still bound to this row, oldest first.

    Each entry is (numeric id, storage key). Both are needed: Telegram wants a
    number, while `replies` is keyed by the message id as a STRING, because
    JSON object keys always are and the caller unbinds by that key.
    """
    found = []
    for message_id, bound in (replies or {}).items():
        if bound != tx_id:
            continue
        try:
            found.append((int(message_id), message_id))
        except (TypeError, ValueError):
            logger.warning(f'Ignoring non-numeric message id {message_id!r}')
    return sorted(found)


def _retire(client, chat_id, replies: dict, tx_id: str, text: str) -> None:
    """Rewrite every outstanding question for a row and unbind it.

    Unbinding matters twice over: a retired question is no longer a valid
    target for a text reply, and leaving it bound would make the next
    retirement edit it again with identical text - which Telegram rejects as
    "message is not modified".

    Best effort throughout: `edit_message` degrades to a falsy return, so a
    question that cannot be rewritten costs the owner feedback and never
    correctness. Nothing here may raise - it runs on the path that has already
    written to the sheet.
    """
    if not client or not chat_id:
        return
    for message_id, key in _messages_for(replies, tx_id):
        client.edit_message(chat_id, message_id, text)
        replies.pop(key, None)


def _write_row(sheets, row: dict, label: str) -> None:
    """Write the category, and the Type too when the row stops being internal.

    An own-card transfer the owner calls anything other than `OWN_CARDS` is not
    an internal move: for a credit card the money leaves and never comes back,
    and the repayment is the only record that it was ever spent. Analytics
    classifies by the Type column, so writing the category alone would leave
    the row dropped from every report exactly as it was.
    """
    sheets.set_category(row["row"], label)
    if row.get("kind") == "own_card" and label != OWN_CARDS:
        sheets.set_flow(row["row"], EXPENSE)


def _learn_key(row: dict) -> str:
    """The Sheet2 rule key that will silence this counterparty in future.

    For a card that is the `*NNNN` tail, which `is_learnable` deliberately
    rejects - four digits and no letters is the exact shape it keeps out of
    substring matching - but which is also exactly what `apply_override`
    matches a card on. Hence a separate key rather than a loosened rule.
    """
    if row.get("kind") == "own_card":
        return _card_tail(row.get("merchant", ""))
    merchant = (row.get("merchant") or "").strip()
    return merchant if is_learnable(merchant) else ""


def _apply(sheets, row, tx_id: str, label: str, applied: list,
           client=None, chat_id=None, replies: dict | None = None) -> bool:
    """Write the category, acknowledge it, learn the rule, record it as applied.

    True iff the sheet write succeeded. `applied` is appended only then: the
    confirmation must never claim a category that failed to land.
    """
    try:
        _write_row(sheets, row, label)
    except Exception as error:
        # One bad row (429, 500, ...) must not abort the batch or lose the
        # offset - both would cause the same update to be redrained (and
        # potentially re-applied) next run.
        logger.error(f'Failed to apply answer for {tx_id}: {error}')
        if client and chat_id:
            client.send_message(chat_id, f'Не вдалося записати «{label}». '
                                         f'Спробую ще раз пізніше.')
        return False

    applied.append({"tx_id": tx_id, "label": label,
                    "merchant": row.get("merchant", "")})
    # The acknowledgement the button cannot give, written onto the question
    # itself so it is unambiguous which row was categorised - and so the
    # keyboard goes away and the question stops looking unanswered.
    merchant = (row.get("merchant") or "").strip() or "—"
    _retire(client, chat_id, replies, tx_id, f'✅ {merchant} → {label}')

    # Deliberately outside the write above: a failed rule is a lost shortcut
    # for future transactions, not a failed answer, and reporting it as one
    # told the owner their category had not been saved when it had.
    key = _learn_key(row)
    if key:
        try:
            sheets.append_rule(key, label)
            logger.info(f'Learned rule for {key!r} -> {label}')
        except Exception as error:
            logger.warning(f'Category saved, but the rule for {tx_id} was not: {error}')
    return True


def _apply_siblings(sheets, row: dict, label: str, pending_by_id: dict,
                    applied: list, client=None, chat_id=None,
                    replies: dict | None = None) -> None:
    """Carry an answer to the other queued rows of the same counterparty.

    The rule an answer writes silences the counterparty, so without this the
    owner's other transfers to that same card - or payments to that same ФОП -
    would keep their old label forever and never be asked about again. Failures
    are logged and swallowed - the answer the owner gave has already landed on
    the row they were asked about, and reporting a partial success as a failure
    is the mistake `append_rule` was moved out of the try block to avoid.
    """
    for sibling in siblings_for(row, label, pending_by_id):
        try:
            _write_row(sheets, sibling, label)
        except Exception as error:
            logger.warning(f'Answer applied, but sibling row '
                           f'{sibling.get("row")} was not updated: {error}')
            continue
        applied.append({"tx_id": sibling.get("tx_id", ""), "label": label,
                        "merchant": sibling.get("merchant", "")})
        _retire(client, chat_id, replies, sibling.get("tx_id", ""),
                f'✅ {sibling.get("merchant", "—")} → {label}')


def drain_answers(client, sheets, state: dict, pending_by_id: dict,
                  choices: list | None = None) -> dict:
    """Apply every answer received since the stored offset.

    The offset advances past every update, including ones we cannot act on -
    otherwise a single malformed update would be redelivered forever.
    """
    choices = choices or CATEGORY_CHOICES
    offset = state.get("offset", 0)
    chat_id = state.get("chat_id")
    declined = list(state.get("declined", []))
    # What actually reached the sheet this run, so the caller can confirm it.
    # Transient: `serialize_state` whitelists the four persisted keys, so this
    # never reaches storage.
    applied = []

    updates = client.get_updates(offset)
    for update in updates:
        offset = max(offset, update.get("update_id", 0) + 1)

        message = update.get("message") or {}
        if str(message.get("text", "")).startswith("/start"):
            incoming = (message.get("chat") or {}).get("id")
            if incoming is None:
                logger.warning('/start with no chat id; ignored')
                continue
            if chat_id is None:
                chat_id = incoming
                logger.info('Telegram chat id discovered')
            elif incoming != chat_id:
                logger.warning('Ignoring /start from an unexpected chat')
            else:
                logger.info('Chat id already known; /start ignored')
            continue

        command = str(message.get("text", "")).strip().lower()
        if command in ("/week", "/month"):
            incoming = (message.get("chat") or {}).get("id")
            # Only the known owner. The same rule that stops a stranger's
            # /start from hijacking the bot applies here - the reply carries
            # the owner's financial data.
            if chat_id is None or incoming != chat_id:
                logger.warning('Ignoring a report command from an unexpected chat')
                continue
            # Deliberately does not touch last_weekly/last_monthly: a manual
            # request must never consume the scheduled report.
            #
            # Swallowed on purpose. An exception here would propagate out of
            # drain_answers before the offset is persisted, so the same command
            # would be redelivered and raise again on every subsequent run -
            # wedging the tagging loop permanently over one failed report.
            try:
                digest.send_digest(sheets, client, chat_id,
                                   "week" if command == "/week" else "month",
                                   date.today())
            except Exception as error:
                logger.error(f'On-demand {command} report failed: {error}')
            continue

        body = str(message.get("text", "")).strip()
        if body and not body.startswith("/"):
            incoming = (message.get("chat") or {}).get("id")
            if chat_id is None or incoming != chat_id:
                logger.warning('Ignoring a message from an unexpected chat')
                continue

            reply_to = (message.get("reply_to_message") or {}).get("message_id")
            tx_id = state.get("replies", {}).get(str(reply_to)) if reply_to else None
            if not tx_id:
                # Never guessed. With up to eight questions outstanding a guess
                # is wrong eventually, and it would write a category to the
                # wrong transaction with nothing able to detect it.
                client.send_message(chat_id, 'Відповідайте на конкретне питання: '
                                             'свайп по ньому → Reply.')
                continue

            row = pending_by_id.get(tx_id)
            if not row:
                logger.info(f'Reply for {tx_id} no longer pending; ignored')
                client.send_message(chat_id, 'Це вже категоризовано.')
                continue

            label = match_text(body, choices)
            # A local match is deterministic and reuses the parser's own
            # vocabulary, so it is applied straight away. A model's answer is a
            # guess and is always confirmed - the owner typed "Кредит", the
            # model returned "Переказ рідним", and because that label happened
            # to be on the list it was written to the sheet with nothing on
            # screen to catch it.
            guessed = False
            if not label:
                label = openrouter_client.propose_category(
                    body, row.get("merchant", ""), choices,
                    os.getenv('OPENROUTER_API_KEY', ''))
                guessed = True
            if not label:
                client.send_message(chat_id, 'Не розпізнав. Спробуйте інакше '
                                             'або оберіть кнопкою.')
                continue

            if guessed or label not in choices:
                # Needs confirming before it lands - and, when the label is new,
                # before it becomes a category forever.
                state.setdefault("proposed", {})[tx_id] = label
                question = (f'Записати «{label}»?' if label in choices
                            else f'Створити категорію «{label}»?')
                if guessed:
                    # Plain text, no markup: send_message is called without a
                    # parse_mode here, so tags would render as literal tags.
                    question += '\n(здогадка моделі за вашим описом)'
                client.send_message(
                    chat_id, question,
                    [[{"text": f'Так — {label}',
                       "callback_data": encode_callback(tx_id, label)}]])
                continue

            # setdefault, not get: _retire unbinds the question it rewrites,
            # and that has to happen on the dict the state actually carries.
            _apply(sheets, row, tx_id, label, applied, client, chat_id,
                   state.setdefault("replies", {}))
            continue

        query = update.get("callback_query")
        if not query:
            continue

        # Always answer, even for a chat we're about to reject - otherwise the
        # tapper's button spinner hangs forever.
        client.answer_callback(query.get("id"))

        query_chat = (query.get("message") or {}).get("chat") or {}
        query_chat_id = query_chat.get("id")
        if chat_id is None or query_chat_id != chat_id:
            logger.warning('Ignoring callback from an unrecognised chat')
            continue

        decoded = decode_callback(query.get("data", ""))
        if not decoded:
            logger.warning('Ignoring malformed callback data')
            continue

        tx_id, label = decoded
        message_id = (query.get("message") or {}).get("message_id")
        replies = state.setdefault("replies", {})
        row = pending_by_id.get(tx_id)
        if not row:
            # Already answered, or resolved in the sheet by hand since we asked.
            # Usually a tap on a stale copy of a question answered elsewhere.
            # Said out loud rather than dropped in silence: that silence is
            # precisely what read as "my answer was not recorded".
            logger.info(f'Answer for {tx_id} no longer pending; ignored')
            if message_id is not None:
                client.edit_message(chat_id, message_id, '✅ Це вже категоризовано.')
            continue
        if label == SKIP_LABEL:
            logger.info(f'{tx_id} skipped; it will not be asked about again')
            if tx_id not in declined:
                declined.append(tx_id)
            merchant = (row.get("merchant") or "").strip() or "—"
            _retire(client, chat_id, replies, tx_id, f'⏭ Пропущено: {merchant}')
            continue
        if label == OTHER_LABEL:
            # NOT a decline. The row stays in the queue until the owner names
            # it, and the instruction is written onto the very message they
            # have to swipe, with the buttons left in place in case they
            # change their mind.
            logger.info(f'{tx_id} answered "{OTHER_LABEL}"; awaiting a typed answer')
            prompt = ('✍️ Відповідайте на це повідомлення (свайп → Reply) '
                      'і напишіть категорію своїми словами.')
            if message_id is None:
                client.send_message(chat_id, prompt)
            else:
                client.edit_message(chat_id, message_id,
                                    f'{format_question(row)}\n\n{prompt}',
                                    build_keyboard(tx_id,
                                                   choices_for(row, choices)))
            continue
        proposed = state.setdefault("proposed", {})
        # The same list the question offered, or a button the bot drew itself
        # is rejected the moment it is tapped.
        row_choices = choices_for(row, choices)
        if label not in row_choices:
            if proposed.get(tx_id) != label:
                # A renamed, removed or no-longer-offered category must fail
                # safe. Only the proposal made for THIS row authorises a label
                # that is not on the list, so a stale confirm button from an
                # earlier conversation cannot inject one.
                logger.warning(f'Unknown category label {label!r}; ignored')
                continue
            sheets.append_button_category(label)

        if _apply(sheets, row, tx_id, label, applied, client, chat_id, replies):
            proposed.pop(tx_id, None)
            _apply_siblings(sheets, row, label, pending_by_id,
                            applied, client, chat_id, replies)

    if offset != state.get("offset", 0):
        logger.info(f'Offset advanced {state.get("offset", 0)} -> {offset}; '
                    f'{len(applied)} answer(s) applied')

    # Every persisted key is carried through, not just the ones this function
    # touches: rebuilding the dict from scratch silently dropped the reply and
    # proposal ledgers, and serialize_state then raised on the missing key.
    return {"offset": offset, "chat_id": chat_id,
            "asked": dict(state.get("asked", {})), "declined": declined,
            "replies": dict(state.get("replies", {})),
            "proposed": dict(state.get("proposed", {})),
            "ask_count": dict(state.get("ask_count", {})),
            "undelivered_alert_at": state.get("undelivered_alert_at", 0),
            # Transient, like `applied`: how many updates Telegram actually
            # handed us, which is what tells the watchdog apart from an idle
            # owner. Whitelisted out of storage by serialize_state.
            "received": len(updates),
            "applied": applied}


def ask_pending(client, chat_id, pending: list, state: dict,
                choices: list | None = None) -> int:
    """Ask about pending rows not declined or asked within the cooldown.

    Returns the number of questions ACTUALLY sent (not len(pending)): a
    falsy send_message stops the loop so we don't hammer a chat already
    being rate-limited, and callers must know the true count to log it.
    """
    if not chat_id:
        logger.info('No Telegram chat id yet; send /start to the bot. Staying silent.')
        return 0

    declined = set(state.get("declined", []))
    asked = state.setdefault("asked", {})
    counts = state.setdefault("ask_count", {})
    now = time.time()
    # The cooldown is per COUNTERPARTY, not per row. Keying it on tx_id alone
    # let the next run skip the *2222 row it had just asked about and promote
    # the next-largest *2222 row into its place, so the owner was asked about
    # the same card again ten minutes later. One answer closes the card either
    # way, so the second question was never going to learn anything.
    last_asked = {}
    for row in pending:
        when = asked.get(row["tx_id"])
        if when is None:
            continue
        key = _cooldown_key(row)
        last_asked[key] = max(last_asked.get(key, 0), when)

    ready = [row for row in pending
             if row["tx_id"] not in declined
             and (now - last_asked.get(_cooldown_key(row), 0)) >= _ASK_COOLDOWN_SECONDS]
    to_ask = [row for row in ready
              if counts.get(row["tx_id"], 0) < MAX_ASKS_PER_ROW]

    # Never a silent cap: a row that has used up its questions stops being
    # asked about but stays pending, and the only place that is visible is here.
    exhausted = len(ready) - len(to_ask)
    if exhausted:
        logger.info(f'{exhausted} row(s) have been asked about '
                    f'{MAX_ASKS_PER_ROW} times and are now quiet')

    if not to_ask:
        return 0

    def _amount(row):
        try:
            return float(row.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Biggest first, and only a handful per run. Once transfers joined the
    # queue 37 rows became askable at once, which buries the few worth
    # answering under a wall of small ones. The rest come on the next run.
    to_ask = sorted(to_ask, key=_amount, reverse=True)

    # One question per counterparty. A single answer closes every queued row
    # that shares a card or a payee (see `siblings_for`), so a second question
    # about the same one cannot tell us anything - and asking it anyway is
    # precisely the "I answer the same question over and over" this design
    # exists to end. Fifteen own-card rows across four cards would otherwise
    # have arrived as five consecutive questions about *2222.
    # Sorted first, so the row that represents the group is the largest one.
    seen = set()
    grouped = []
    for row in to_ask:
        # Rows with no reusable key (the bank never names the recipient) fall
        # back to their own id, so they are never collapsed into each other.
        key = _cooldown_key(row)
        if key in seen:
            continue
        seen.add(key)
        grouped.append(row)
    to_ask = grouped

    backlog = len(to_ask)
    to_ask = to_ask[:MAX_QUESTIONS_PER_RUN]

    head = f'Питань зараз: {len(to_ask)}'
    if backlog > len(to_ask):
        head += f' (у черзі {backlog})'
    summary = [head, '']
    for row in to_ask:
        summary.append(f'• {row.get("date", "")} {row.get("amount", "")}₴ '
                       f'{row.get("merchant", "")}')
    if not client.send_message(chat_id, "\n".join(summary)):
        logger.warning('Telegram summary send failed; skipping questions this run')
        return 0

    replies = state.setdefault("replies", {})
    sent = 0
    for row in to_ask:
        # Exactly one live question per row. Without this the chat accumulated
        # an identical question every cooldown window, each with a working
        # keyboard - and answering any but the newest did nothing and said
        # nothing, which is what "I answer the same question three times" was.
        _retire(client, chat_id, replies, row["tx_id"],
                f'↻ Це питання перепитано нижче '
                f'({(row.get("merchant") or "").strip() or "—"}).')
        time.sleep(SEND_PAUSE_SECONDS)
        result = client.send_message(chat_id, format_question(row),
                                     build_keyboard(row["tx_id"],
                                                    choices_for(row, choices)))
        if not result:
            logger.warning(f'Telegram send failed after {sent} of {len(to_ask)} '
                           f'question(s); stopping')
            break
        # Bind the message to its row so a reply can be attributed exactly.
        # String key: JSON object keys are strings, and an int would round-trip
        # to one and never match again.
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is not None:
            replies[str(message_id)] = row["tx_id"]
        asked[row["tx_id"]] = now
        counts[row["tx_id"]] = counts.get(row["tx_id"], 0) + 1
        sent += 1

    return sent


def prune_state(state: dict, still_pending: set) -> None:
    """Drop ledger entries for rows that have left the queue.

    The state lives in one spreadsheet cell, so unbounded growth is a failure
    mode rather than untidiness.
    """
    state["asked"] = {tx: ts for tx, ts in state.get("asked", {}).items()
                      if tx in still_pending}
    state["declined"] = [tx for tx in state.get("declined", [])
                         if tx in still_pending]
    state["replies"] = {mid: tx for mid, tx in state.get("replies", {}).items()
                        if tx in still_pending}
    state["proposed"] = {tx: label for tx, label in state.get("proposed", {}).items()
                         if tx in still_pending}
    state["ask_count"] = {tx: n for tx, n in state.get("ask_count", {}).items()
                          if tx in still_pending}


def run_tagging(sheets, token: str, store=None) -> None:
    """Drain answers, then ask about whatever is still unresolved.

    Wrapped end to end: a Telegram or Sheets failure here must never affect the
    transaction pipeline, which has already committed its rows by this point.

    `store` defaults to the spreadsheet, which is the only backend that
    survives a host with an ephemeral filesystem. A plain string is accepted
    as a path to a local file, for local runs and tests.
    """
    if not token:
        logger.info('TELEGRAM_API not set; skipping the tagging loop.')
        return
    if store is None:
        store = SheetStore(sheets)
    elif isinstance(store, str):
        store = FileStore(store)
    try:
        client = TelegramClient(token)
        state = parse_state(store.read())
        pending = sheets.pending_rows()
        pending_by_id = {row["tx_id"]: row for row in pending}

        sheet_labels = sanitize_choices(sheets.button_categories())
        if sheet_labels:
            # The owner's own order in the Категорії tab is deliberate.
            choices = sheet_labels
        else:
            # No curated list: fall back to the built-ins, most-used first.
            frequencies = sheets.category_frequencies()
            choices = sorted(CATEGORY_CHOICES,
                             key=lambda c: (-frequencies.get(c, 0), c))
        logger.info(f'Offering {len(choices)} category buttons')

        state = drain_answers(client, sheets, state, pending_by_id, choices)
        applied = state.pop("applied", [])
        received = state.pop("received", 0)
        # Persist immediately: if the Sheets calls below (pending_rows,
        # category_frequencies) fail, the drained offset must still survive so
        # the next run doesn't re-drain (and potentially re-apply) this batch.
        store.write(serialize_state(state))

        # Acknowledge the taps. Sent after the state write, so a Telegram
        # failure here cannot cost us the drained offset; send_message already
        # degrades to a falsy return rather than raising.
        if applied and state["chat_id"]:
            client.send_message(state["chat_id"], format_confirmation(applied))

        # Runs after the offset is safely written, and reports the one failure
        # mode nothing else can see: an answer Telegram took and we never got.
        # The cooldown it stamps is persisted by the final write below; losing
        # it costs one repeated warning, which is the harmless direction.
        alarm = check_undelivered(client, state, received, time.time())
        if alarm and state["chat_id"]:
            client.send_message(state["chat_id"], alarm)

        # Re-read: the answers just applied have shrunk the pending set, and we
        # must not ask again about a row that was answered moments ago.
        still_pending = sheets.pending_rows()
        still_pending_ids = {row["tx_id"] for row in still_pending}

        # Prune entries for tx_ids no longer pending (resolved by hand,
        # answered, or removed) so the state cell cannot grow forever.
        prune_state(state, still_pending_ids)

        sent = ask_pending(client, state["chat_id"], still_pending, state, choices)
        store.write(serialize_state(state))
        logger.info(f'Telegram tagging: {sent} question(s) sent')
    except Exception as error:
        logger.error(f'Telegram tagging failed, pipeline unaffected: {error}')

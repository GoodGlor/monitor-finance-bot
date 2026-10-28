"""The weekly and monthly digest: read, compute, format, send.

Deliberately does NOT import telegram_service - that module imports this one to
serve the /week and /month commands, and a module-level import back would be a
cycle.
"""
import html
import json
import logging
import os
import re
from datetime import date

import analytics
import openrouter_client
from categories import scrub_merchant
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

# Exactly what is persisted. Anything else a future version puts on the state
# dict stays in memory rather than leaking into the cell.
STATE_KEYS = ("last_weekly", "last_monthly", "model")


def parse_state(text: str | None) -> dict:
    """The stored JSON, or defaults. Never raises on malformed input.

    A digest that crashes on a corrupt state cell would stop reporting entirely;
    forgetting which period was last sent costs at most one duplicate message.
    """
    state = {key: "" for key in STATE_KEYS}
    if not text or not text.strip():
        return state
    try:
        stored = json.loads(text)
    except (ValueError, TypeError):
        return state
    if not isinstance(stored, dict):
        return state
    for key in STATE_KEYS:
        value = stored.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            state[key] = str(value)
    return state


def serialize_state(state: dict) -> str:
    return json.dumps({key: str(state.get(key) or "") for key in STATE_KEYS},
                      ensure_ascii=False)


MAX_MESSAGE = 4096

# Enough rows to be useful, few enough that the message never needs splitting.
MAX_CATEGORY_LINES = 12
MAX_RECURRING_LINES = 8

# Column widths for the <pre> tables. Kept under 32 characters total so the
# monospace block does not wrap on a phone - a wrapped table is worse than no
# table, because the amount ends up on its own line.
NAME_WIDTH = 17
AMOUNT_WIDTH = 7

MONTHS_UA = ("січ", "лют", "бер", "кві", "тра", "чер",
             "лип", "сер", "вер", "жов", "лис", "гру")
MONTHS_FULL = ("Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
               "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень")


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _day(value) -> str:
    return f"{value.day} {MONTHS_UA[value.month - 1]}"


def _clean(name: str, width: int = NAME_WIDTH) -> str:
    """A merchant or category as one tidy column cell.

    The bank pads names with runs of spaces and trails them with card and
    contract numbers, so the raw text arrives as
    "Кредит готівкою 12345678901234" or "SALONBELLA  24.24.24.24.24 AVE".
    """
    collapsed = " ".join((name or "").split())
    if len(collapsed) > width:
        collapsed = collapsed[:width - 1].rstrip() + "…"
    return collapsed


def _table(pairs: list[tuple], width: int = AMOUNT_WIDTH) -> str:
    """A monospace block of name / value / note rows.

    Everything with a number in it goes through here. Two reasons, both found
    on live data: proportional text ran names into their amounts
    ("Кредит готівкою 25092339 1 432"), and Telegram turned a merchant called
    "24.24.24.24" into a clickable link. Monospace fixes the first and a code
    entity suppresses the second.

    <pre> rather than <code>: Telegram frames a <pre> block with a border and
    a header bar, and that frame is what visually separates one section of the
    report from the next. The copy button that comes with it is the price.
    """
    rows = []
    for name, value, note in pairs:
        cell = f"{_clean(name):<{NAME_WIDTH}} {value:>{width}}"
        rows.append(f"{cell}  {note}".rstrip())
    return f"<pre>{html.escape(chr(10).join(rows))}</pre>"


def _delta(line: dict) -> str:
    if line["is_new"] or line["delta_pct"] is None:
        return "нова"
    sign = "+" if line["delta_pct"] >= 0 else "−"
    return f"{sign}{abs(line['delta_pct']):.0f}%"


def _header(report) -> str:
    if report.period == "week":
        return f"📊 Тиждень {_day(report.start)} – {_day(report.end)}"
    return f"📈 {MONTHS_FULL[report.start.month - 1]} {report.start.year}"


def _days(count: int) -> str:
    """Ukrainian plural for a day count: 1 день, 2 дні, 5 днів, 11 днів."""
    if count % 100 in (11, 12, 13, 14):
        return "днів"
    last = count % 10
    if last == 1:
        return "день"
    if last in (2, 3, 4):
        return "дні"
    return "днів"


def _pace_block(report) -> list[str]:
    """How the pay cycle is going, answered in hryvnia rather than in rates.

    The previous version led with "витрачено 90% доходу, минуло 94% циклу",
    which asks the reader to compare two percentages with different
    denominators and then work out what follows. It also printed a
    forward-looking figure (what is left, per remaining day) beside a
    backward-looking one (the average per day so far) with nothing to say they
    were different questions. The owner read that block and said it did not
    mean anything to them, which is the only test a line of a report has to
    pass. So: the verdict first, then the money, and the percentages dropped.

    `ahead` keeps its old definition on purpose - the same rule drives the
    pace signal in analytics.signals, and two nearly-identical verdicts
    disagreeing inside one message would be worse than the wording ever was.
    """
    pace = report.pace
    if not pace.expected:
        return ["Дохід за цикл невідомий — темп не рахується"]

    left = max(pace.expected - pace.outflow, 0.0)
    ahead = pace.burned_pct - pace.elapsed_pct > 15
    if not left:
        mark, verdict = "⚠️", "Дохід за цикл уже витрачено"
    elif ahead:
        mark, verdict = "⚠️", "Витрачаєте швидше, ніж минає час"
    else:
        mark, verdict = "🟢", "Має вистачити"

    # The cycle is named explicitly. It is a different window from the report
    # period, and side by side ("336 ₴ за тиждень" / "6 097 ₴ лишилось") the
    # two read as a contradiction unless the reader can see they are not the
    # same thing.
    head = f"<b>Цикл {_day(pace.start)} – {_day(pace.end)}</b>"
    if pace.days_left:
        head += f" · лишилось {pace.days_left} {_days(pace.days_left)}"

    lines = [head, "", f"{mark} <b>{verdict}</b>"]
    if left and pace.days_left:
        lines.append(f"Лишилось {_money(left)} ₴, а витрачаєте "
                     f"≈ {_money(pace.current_daily)} ₴/день")
    elif left:
        lines.append(f"Лишилось {_money(left)} ₴")
    label = "Надійшло" if pace.income_received else "Очікується"
    lines.append(f"{label} {_money(pace.expected)} ₴, "
                 f"витрачено {_money(pace.outflow)} ₴")
    return lines


def format_report(report, llm_available: bool = True) -> str:
    """The numeric message.

    Every name+amount pair lives in a <pre> table; plain lines carry only
    prose. That is what keeps columns aligned and stops Telegram linkifying
    merchant names that look like addresses.
    """
    parts = [f"<b>{html.escape(_header(report))}</b>", ""]

    if not report.tx_count:
        parts.append("За цей період транзакцій не було.")
        parts.append("Якщо ви точно щось витрачали — варто перевірити пошту.")
        return "\n".join(parts)[:MAX_MESSAGE]

    totals = report.totals
    # Every figure in this block is derived from the figures printed beside
    # it, not from the underlying floats. Rounding each independently left the
    # visible subtraction one hryvnia out.
    shown_income = round(totals.income)
    shown_spend = round(totals.spend)
    moved = round(totals.cash + totals.transfers_out)
    diff = shown_income - shown_spend - moved
    summary = [("Надійшло", f"{_money(shown_income)} ₴", ""),
               ("Витрати", f"{_money(shown_spend)} ₴", "")]
    if moved:
        # Listed separately so the three lines add up: money moved is not money
        # consumed, but it did leave the account.
        summary.append(("Переміщення", f"{_money(moved)} ₴", ""))
    summary.append(("Різниця", f"{'+' if diff >= 0 else '−'}{_money(abs(diff))} ₴", ""))
    parts.append(_table(summary, width=AMOUNT_WIDTH + 2))

    if report.balance:
        # Labelled with the moment it belongs to, never as "money you have":
        # with several cards the column jumps between them, so this is the
        # balance of whichever card was used last.
        moment = report.balance["at"]
        parts.append("")
        parts.append(f"💳 <b>Залишок {_money(report.balance['amount'])} ₴</b>")
        parts.append(f"<i>після операції {_day(moment)} "
                     f"{moment.strftime('%H:%M')}</i>")
    parts.append("")
    parts += _pace_block(report)

    parts.append("")
    parts.append("<b>Куди пішло</b>")
    shown = report.categories[:MAX_CATEGORY_LINES]
    table = [(line["name"], _money(line["amount"]), _delta(line)) for line in shown]
    rest = report.categories[MAX_CATEGORY_LINES:]
    if rest:
        # Never drop rows silently. Live July had 18 categories; printing 12
        # left 1 758 UAH unaccounted for and the column stopped adding up.
        #
        # Derived from the DISPLAYED figures, not from the raw sum, so the
        # column adds up to the total the reader can see. Rounding twelve lines
        # to whole hryvnia otherwise leaves the visible arithmetic one off.
        residual = round(totals.spend) - sum(round(line["amount"]) for line in shown)
        # Not "Інше": that is the bot's "cannot say" button, and one word
        # meaning two things in the same report confused the owner.
        table.append((f"Решта ({len(rest)})", _money(residual), ""))
    parts.append(_table(table))
    if not report.baseline_ready:
        parts.append("<i>норма ще накопичується</i>")

    if totals.cash or totals.transfers_out:
        parts.append("")
        parts.append("<b>Не витрати, а переміщення</b>")
        moved = []
        if totals.cash:
            moved.append(("Готівка", f"{_money(totals.cash)} ₴", ""))
        if totals.transfers_out:
            moved.append(("Перекази людям", f"{_money(totals.transfers_out)} ₴", ""))
        parts.append(_table(moved, width=AMOUNT_WIDTH + 2))

    if report.period == "month" and report.recurring:
        floor = sum(item.amount for item in report.recurring
                    if item.status != "gone")
        parts.append("")
        parts.append(f"<b>Регулярні платежі</b> — {_money(floor)} ₴/міс")
        marks = {"new": "нове", "gone": "стоп"}
        parts.append(_table([(item.merchant, _money(item.amount),
                              marks.get(item.status, ""))
                             for item in report.recurring[:MAX_RECURRING_LINES]]))

    if report.top_day and report.period == "month":
        parts.append("")
        parts.append(f"Найдорожчий день — {_day(report.top_day['date'])}, "
                     f"{_money(report.top_day['amount'])} ₴")

    parts.append("")
    if report.signals:
        parts.append("<b>На що звернути увагу</b>")
        parts += [f"• {html.escape(signal.text)}" for signal in report.signals]
    else:
        parts.append("✅ Нічого не вибивається з норми")

    if not llm_available:
        parts.append("")
        parts.append("<i>Аналіз від моделі ще не підключено.</i>")

    text = "\n".join(parts)
    if len(text) <= MAX_MESSAGE:
        return text
    # Trim whole lines rather than mid-tag: cutting inside <pre> would send
    # malformed HTML and Telegram would reject the whole message.
    while parts and len("\n".join(parts)) > MAX_MESSAGE:
        parts.pop()
    return "\n".join(parts)


def format_analysis(text: str, model: str) -> str:
    """The second message. Names the model, which is honest and aids diagnosis."""
    short = model.split("/")[-1].replace(":free", "")
    return (f"<b>🧠 Аналіз</b> · {html.escape(short)}\n\n"
            f"{html.escape(text)}")


PERIOD_STATE_KEY = {"week": "last_weekly", "month": "last_monthly"}


def _period_start(period: str, today: date):
    bounds = analytics.week_bounds if period == "week" else analytics.month_bounds
    return bounds(today)[0]


def due_periods(today: date, state: dict) -> list[str]:
    """Which reports today owes, skipping any already sent.

    Weekly on Monday, monthly on the 1st. Both on a 1st that is a Monday - two
    separate messages, because merging them would bury one report inside the
    other.
    """
    due = []
    if today.weekday() == 0:
        due.append("week")
    if today.day == 1:
        due.append("month")
    return [period for period in due
            if analytics.period_key(period, _period_start(period, today))
            != state[PERIOD_STATE_KEY[period]]]


def send_digest(sheets, client, chat_id, period: str, today: date,
                rows=None, state: dict | None = None) -> str | None:
    """Build and send one report. Returns the period key iff it was delivered.

    The analysis is a separate message sent afterwards, so a failure anywhere in
    the OpenRouter path cannot touch delivery of the numbers - which is the
    whole point of the report.
    """
    rows = sheets.all_rows() if rows is None else rows
    report = analytics.build_report(rows, period, today)

    analysis = None
    try:
        analysis = openrouter_client.analyze(
            build_payload(report),
            os.getenv('OPENROUTER_API_KEY', ''),
            prefer=(state or {}).get("model", ""))
    except Exception as error:
        # Swallowed on purpose. analyze() is written not to raise, but this path
        # runs unattended and the numbers matter more than the commentary.
        logger.warning(f'Analysis failed ({error.__class__.__name__}); '
                       f'sending the numbers alone')

    text = format_report(report, llm_available=bool(analysis))
    if not client.send_message(chat_id, text, parse_mode="HTML"):
        logger.warning(f'{period} digest was not delivered; state left untouched')
        return None

    if analysis:
        body, model = analysis
        if state is not None:
            state["model"] = model
        # Best effort: the report has already landed, so a failure here costs
        # the commentary and nothing else.
        client.send_message(chat_id, format_analysis(body, model), parse_mode="HTML")
    return report.key


def run(sheets, token: str, chat_id, today: date | None = None) -> None:
    """One daily pass. Sends whatever is due and records only what landed."""
    today = today or date.today()
    if not chat_id:
        logger.warning('No chat id; the digest has nowhere to go')
        return

    state = parse_state(sheets.read_state(cell=sheets.DIGEST_STATE_RANGE))
    pending = due_periods(today, state)
    if not pending:
        logger.info('Nothing due today')
        return

    client = TelegramClient(token)
    rows = sheets.all_rows()
    for period in pending:
        key = send_digest(sheets, client, chat_id, period, today, rows=rows,
                          state=state)
        if not key:
            continue
        # Written per period, immediately: a failure on the second report must
        # not undo the first one having been delivered.
        state[PERIOD_STATE_KEY[period]] = key
        sheets.write_state(serialize_state(state), cell=sheets.DIGEST_STATE_RANGE)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv('TELEGRAM_API', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    from auth_service import AuthService
    from spread_sheet_service import SpreadSheetService

    try:
        auth = AuthService()
        sheets = SpreadSheetService(auth.service_sheets, auth.spreadsheet_id)
        if not chat_id:
            from telegram_state import SheetStore, parse_state as parse_tg_state
            chat_id = parse_tg_state(SheetStore(sheets).read()).get("chat_id")
        run(sheets, token, chat_id)
    except Exception as error:
        # Report, then re-raise so the scheduler still records the failure.
        # Sent through telegram_client directly rather than
        # telegram_service.notify_failure: that module imports this one for the
        # /week and /month commands, so importing it back would be a cycle. No
        # throttling either - a once-a-day job cannot spam.
        logger.error(f'Digest failed: {error}')
        if token and chat_id:
            TelegramClient(token).send_message(
                chat_id, f'⚠️ Дайджест не сформувався: {error.__class__.__name__}')
        raise


if __name__ == '__main__':
    main()


def build_payload(report) -> dict:
    """Exactly what may leave the machine - the finished aggregates, nothing else.

    No transaction ids, no per-transaction dates, no balance, and no
    per-transaction amounts beyond the single largest. Person-to-person
    transfers go as one total with no names: top merchants were approved for
    venues and services, not for people. `top_merchants` is consumption only,
    which excludes P2P by construction.
    """
    totals = report.totals
    pace = report.pace
    return {
        "period": report.period,
        "start": str(report.start),
        "end": str(report.end),
        "income": round(totals.income),
        "spend": round(totals.spend),
        "moved": round(totals.cash + totals.transfers_out),
        "cycle": {
            "ends": str(pace.end),
            "expected_income": round(pace.expected),
            "outflow": round(pace.outflow),
            "burned_pct": pace.burned_pct,
            "elapsed_pct": pace.elapsed_pct,
            "days_left": pace.days_left,
            "sustainable_daily": round(pace.sustainable_daily),
            "current_daily": round(pace.current_daily),
        },
        "categories": [{"name": line["name"],
                        "amount": round(line["amount"]),
                        "norm": None if line["is_new"] else round(line["avg"]),
                        "delta_pct": line["delta_pct"]}
                       for line in report.categories],
        "top_merchants": [{"name": scrub_merchant(m["name"]),
                           "amount": round(m["amount"]),
                           "category": m["category"]}
                          for m in report.top_merchants],
        "recurring": [{"name": scrub_merchant(item.merchant),
                       "amount": round(item.amount),
                       "status": item.status}
                      for item in report.recurring],
        "biggest": ({"name": scrub_merchant(report.biggest["merchant"]),
                     "amount": round(report.biggest["amount"])}
                    if report.biggest else None),
        "signals": [signal.text for signal in report.signals],
        "unknown_share": report.unknown_share,
    }

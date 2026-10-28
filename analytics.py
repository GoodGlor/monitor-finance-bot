"""Pure computation for the weekly and monthly digest.

Deliberately free of any API object: this module takes plain dicts produced by
SpreadSheetService.all_rows() and returns plain data, so every metric here is
tested without mocking a single network call.

Row shape: {"date": datetime, "kind": str, "category": str,
            "merchant": str, "amount": float}
Amounts are always positive; direction lives in "kind".
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from categories import CASH, TRANSFERS_OUT, UNKNOWN

INCOME = "Дохід"
INTERNAL = "Переказ"

def classify(row: dict) -> str:
    """Which flow group a row belongs to. Exactly one, never more."""
    kind = (row.get("kind") or "").strip()
    category = (row.get("category") or "").strip()
    if kind == INTERNAL:
        return "internal"
    if kind == INCOME:
        return "income"
    if category == CASH:
        return "cash"
    if category in TRANSFERS_OUT:
        return "transfers_out"
    # Anything else, including a row with no Type at all, is an outflow.
    return "spend"


@dataclass(frozen=True)
class Totals:
    income: float = 0.0
    spend: float = 0.0
    cash: float = 0.0
    transfers_out: float = 0.0

    @property
    def total_outflow(self) -> float:
        """Money that left the account. This is what income is compared to."""
        return round(self.spend + self.cash + self.transfers_out, 2)


def totals(rows: list[dict]) -> Totals:
    acc = {"income": 0.0, "spend": 0.0, "cash": 0.0, "transfers_out": 0.0}
    for row in rows:
        group = classify(row)
        if group == "internal":
            continue
        acc[group] += float(row.get("amount") or 0.0)
    return Totals(**{k: round(v, 2) for k, v in acc.items()})


def week_bounds(today: date) -> tuple[date, date]:
    """The completed Mon-Sun week before `today`.

    Always the previous week, never the running one: a report on a
    half-finished week would compare four days against a seven-day baseline.
    """
    start = today - timedelta(days=today.weekday() + 7)
    return start, start + timedelta(days=6)


def month_bounds(today: date) -> tuple[date, date]:
    """The previous calendar month. Handles February and year rollover."""
    last_day_prev = today.replace(day=1) - timedelta(days=1)
    return last_day_prev.replace(day=1), last_day_prev


def period_key(period: str, start: date) -> str:
    """The idempotency key stored in state. One period, one key, forever."""
    if period == "week":
        iso = start.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{start.year}-{start.month:02d}"


def rows_between(rows: list[dict], start: date, end: date) -> list[dict]:
    """Rows whose date falls in [start, end], both ends inclusive."""
    return [r for r in rows if start <= r["date"].date() <= end]


# Average weeks in a month. The weekly baseline is the monthly one divided by
# this, so a weekly report can be compared against the same three months.
WEEKS_PER_MONTH = 4.33

# A baseline built from a single observation is noise, not a norm. Such a
# category is reported as new rather than given a meaningless percentage.
MIN_BASELINE_OBSERVATIONS = 2


def by_category(rows: list[dict]) -> dict[str, float]:
    """Consumption only, summed per category."""
    out: dict[str, float] = {}
    for row in rows:
        if classify(row) != "spend":
            continue
        category = row["category"] or ""
        out[category] = out.get(category, 0.0) + float(row.get("amount") or 0.0)
    return {k: round(v, 2) for k, v in out.items()}


def top_merchants(rows: list[dict], limit: int = 8) -> list[dict]:
    """Biggest consumption merchants, summed per name.

    Category totals cannot carry advice on their own: "Кафе +41%" says less
    than "Glovo 1 800, half of that category". Consumption only - cash and P2P
    transfers are excluded here as everywhere else, and that exclusion also
    keeps people's names out of everything downstream.
    """
    sums: dict[str, float] = {}
    where: dict[str, str] = {}
    for row in rows:
        if classify(row) != "spend":
            continue
        name = (row.get("merchant") or "").strip()
        if not name:
            continue
        sums[name] = sums.get(name, 0.0) + float(row.get("amount") or 0.0)
        where.setdefault(name, row.get("category") or "")
    ranked = sorted(sums.items(), key=lambda pair: pair[1], reverse=True)[:limit]
    return [{"name": name, "amount": round(total, 2), "category": where[name]}
            for name, total in ranked]


def baseline_window(today: date, months: int = 3) -> tuple[date, date]:
    """The N complete calendar months before the one `today` falls in.

    The running month is excluded on purpose: comparing a finished period
    against a half-finished average would flatter every report.
    """
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    return start, end


def baseline(rows: list[dict], today: date, months: int = 3,
             weekly: bool = False) -> dict[str, dict]:
    """Per-category norm: {"avg": per-period average, "seen": observations}."""
    start, end = baseline_window(today, months)
    sums: dict[str, float] = {}
    seen: dict[str, int] = {}
    for row in rows_between(rows, start, end):
        if classify(row) != "spend":
            continue
        category = row["category"] or ""
        sums[category] = sums.get(category, 0.0) + float(row.get("amount") or 0.0)
        seen[category] = seen.get(category, 0) + 1
    divisor = months * (WEEKS_PER_MONTH if weekly else 1.0)
    return {c: {"avg": round(total / divisor, 2), "seen": seen[c]}
            for c, total in sums.items()}


def category_lines(actual: dict[str, float], base: dict[str, dict]) -> list[dict]:
    """Merge this period's spend with the norm, biggest first."""
    lines = []
    for name, amount in actual.items():
        norm = base.get(name)
        is_new = (norm is None or norm["seen"] < MIN_BASELINE_OBSERVATIONS
                  or not norm["avg"])
        lines.append({
            "name": name,
            "amount": amount,
            "avg": None if is_new else norm["avg"],
            "delta_pct": None if is_new else round((amount - norm["avg"]) / norm["avg"] * 100, 1),
            "is_new": is_new,
        })
    return sorted(lines, key=lambda line: line["amount"], reverse=True)


# A credit smaller than this share of a typical month's income is a refund or a
# stray P2P transfer, not a salary. Without the filter, a 50 UAH refund arriving
# on the 2nd of every month reads as a second pay day.
INCOME_NOISE_FLOOR = 0.25

# Two pay dates 7 days apart are the same event drifting; 15 days apart are an
# advance and a salary.
CLUSTER_GAP_DAYS = 7

# A cycle claimed from two sightings is a coincidence.
CYCLE_MIN_MONTHS = 3
CYCLE_RECENT_MONTHS = 4


@dataclass(frozen=True)
class IncomeCycle:
    kind: str                      # "monthly" | "semimonthly" | "irregular"
    anchor_days: tuple = ()
    expected: float = 0.0
    next_at: date | None = None


def _month_key(moment) -> tuple:
    return (moment.year, moment.month)


def _recent_month_keys(today: date, count: int) -> set:
    keys, cursor = set(), today.replace(day=1)
    for _ in range(count):
        cursor = cursor - timedelta(days=1)
        keys.add(_month_key(cursor))
        cursor = cursor.replace(day=1)
    return keys


def _cluster_days(days: list[int]) -> list[list[int]]:
    """Split sorted days of the month at gaps wider than CLUSTER_GAP_DAYS."""
    clusters: list[list[int]] = []
    for day in days:
        if clusters and day - clusters[-1][-1] <= CLUSTER_GAP_DAYS:
            clusters[-1].append(day)
        else:
            clusters.append([day])
    return clusters


def _next_anchor(today: date, anchors: tuple) -> date | None:
    candidates = []
    for offset in (0, 1):
        year, month = today.year, today.month + offset
        if month > 12:
            year, month = year + 1, month - 12
        last = calendar.monthrange(year, month)[1]
        candidates += [date(year, month, min(day, last)) for day in anchors]
    future = [c for c in candidates if c > today]
    return min(future) if future else None


def detect_income_cycle(rows: list[dict], today: date,
                        lookback_months: int = 6) -> IncomeCycle:
    """Work out when income arrives, from the history alone.

    Deliberately blunt: cluster the days of the month on which meaningful
    credits landed, and only believe a cluster that shows up in most of the
    recent months. Anything less certain is reported as irregular, which is a
    valid answer rather than a failure - pace then measures against the average
    monthly income instead of against a date.
    """
    start, end = baseline_window(today, lookback_months)
    credits = [r for r in rows_between(rows, start, end) if classify(r) == "income"]
    if not credits:
        return IncomeCycle("irregular")

    per_month: dict[tuple, float] = {}
    for credit in credits:
        key = _month_key(credit["date"])
        per_month[key] = per_month.get(key, 0.0) + float(credit["amount"])
    expected = round(median(per_month.values()), 2)

    floor = INCOME_NOISE_FLOOR * expected
    big = [c for c in credits if float(c["amount"]) >= floor]
    if len(per_month) < 2 or not big:
        return IncomeCycle("irregular", (), expected, None)

    recent = _recent_month_keys(today, CYCLE_RECENT_MONTHS)
    kept = []
    for cluster in _cluster_days(sorted({c["date"].day for c in big})):
        members = [c for c in big if c["date"].day in cluster]
        months = {_month_key(m["date"]) for m in members}
        if len(months & recent) >= CYCLE_MIN_MONTHS:
            kept.append(int(median(sorted(m["date"].day for m in members))))

    if not kept:
        return IncomeCycle("irregular", (), expected, None)
    anchors = tuple(sorted(kept)[:2])
    kind = "monthly" if len(anchors) == 1 else "semimonthly"
    return IncomeCycle(kind, anchors, expected, _next_anchor(today, anchors))


@dataclass(frozen=True)
class Pace:
    start: date
    end: date
    elapsed_pct: float
    burned_pct: float
    days_left: int
    income: float
    expected: float
    outflow: float
    current_daily: float
    sustainable_daily: float
    runway_gap: int
    income_received: bool


def _cycle_window(cycle: IncomeCycle, today: date) -> tuple[date, date]:
    """Start and end of the pay cycle `today` falls in.

    An undetectable cadence falls back to the calendar month, which is the
    honest answer rather than a guessed pay date.
    """
    if cycle.kind == "irregular" or not cycle.anchor_days:
        start = today.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)

    anchors = []
    for offset in (-1, 0, 1):
        year, month = today.year, today.month + offset
        if month < 1:
            year, month = year - 1, month + 12
        elif month > 12:
            year, month = year + 1, month - 12
        last = calendar.monthrange(year, month)[1]
        anchors += [date(year, month, min(day, last)) for day in cycle.anchor_days]
    anchors.sort()

    past = [a for a in anchors if a <= today]
    future = [a for a in anchors if a > today]
    start = past[-1] if past else today.replace(day=1)
    end = future[0] if future else today + timedelta(days=30)
    return start, end


def pace(rows: list[dict], cycle: IncomeCycle, today: date) -> Pace:
    """How fast income is being spent, relative to how much of the cycle is gone.

    Compares two rates rather than a remaining sum, so it never touches the
    balance column - which cannot be trusted here, because the card is not
    stored and the column jumps between cards.
    """
    start, end = _cycle_window(cycle, today)
    window = rows_between(rows, start, min(today, end))
    figures = totals(window)

    received = figures.income > 0
    expected = figures.income if received else cycle.expected
    outflow = figures.total_outflow

    span = max((end - start).days, 1)
    elapsed = max((today - start).days + 1, 1)
    days_left = max((end - today).days, 0)

    current_daily = round(outflow / elapsed, 2)
    left = max(expected - outflow, 0.0)
    sustainable_daily = round(left / days_left, 2) if days_left else 0.0
    lasts = left / current_daily if current_daily else float(days_left)

    return Pace(
        start=start,
        end=end,
        elapsed_pct=round(elapsed / span * 100, 1),
        burned_pct=round(outflow / expected * 100, 1) if expected else 0.0,
        days_left=days_left,
        income=figures.income,
        expected=round(expected, 2),
        outflow=outflow,
        current_daily=current_daily,
        sustainable_daily=sustainable_daily,
        runway_gap=int(round(lasts - days_left)),
        income_received=received,
    )


# A subscription's amount drifts with tariffs and exchange rates; a grocery run
# does not repeat to within a sixth of its size.
RECURRING_TOLERANCE = 0.15
RECURRING_WINDOW_MONTHS = 4
RECURRING_MIN_MONTHS = 3

# An 8 UAH metro fare repeats every month too. Below this it is a habit, not a
# subscription, and listing it drowns the ones worth cancelling.
RECURRING_MIN_UAH = 100.0

# Two consecutive months is not proof, but it is worth telling the owner about:
# a subscription that started last month is exactly what they want to catch.
RECURRING_NEW_MONTHS = 2


@dataclass(frozen=True)
class Recurring:
    merchant: str
    amount: float
    months: int
    status: str


def recurring(rows: list[dict], today: date, period_rows: list[dict]) -> list[Recurring]:
    """Merchants that charge a similar amount month after month.

    `period_rows` is the reporting period, used only to decide whether an
    established charge is still running - a subscription that stopped is worth
    reporting too, because it usually means terms changed.
    """
    start, end = baseline_window(today, RECURRING_WINDOW_MONTHS)
    per_merchant: dict[str, dict] = {}
    for candidate in rows_between(rows, start, end):
        if classify(candidate) != "spend":
            continue
        name = candidate["merchant"]
        if not name:
            continue
        bucket = per_merchant.setdefault(name, {"amounts": [], "months": set()})
        bucket["amounts"].append(float(candidate["amount"]))
        bucket["months"].add(_month_key(candidate["date"]))

    seen_in_period = {r["merchant"] for r in period_rows}
    found = []
    for name, bucket in per_merchant.items():
        months = len(bucket["months"])
        if months < RECURRING_NEW_MONTHS:
            continue
        typical = median(bucket["amounts"])
        if typical < RECURRING_MIN_UAH:
            continue
        spread = max(abs(a - typical) / typical for a in bucket["amounts"])
        if spread > RECURRING_TOLERANCE:
            continue
        if months >= RECURRING_MIN_MONTHS:
            status = "active" if name in seen_in_period else "gone"
        else:
            status = "new"
        found.append(Recurring(name, round(typical, 2), months, status))
    return sorted(found, key=lambda item: item.amount, reverse=True)


# Doubled thresholds on purpose. A percentage alone fires on +80% of 40 UAH,
# which is noise; an absolute alone fires on every large stable category.
CATEGORY_RISE_PCT = 30.0
CATEGORY_RISE_UAH = 300.0

# Pace this far ahead of the cycle is the difference between "a big week" and
# "the money runs out early".
PACE_AHEAD_POINTS = 15.0

BIG_PURCHASE_MULTIPLE = 3.0
# Three times the median of a quiet week can be 129 UAH. Same reasoning as the
# category thresholds: a multiple alone is not a signal.
BIG_PURCHASE_MIN_UAH = 500.0
UNKNOWN_SHARE_LIMIT = 0.10

# Instalments smear one purchase across months, so the period they land in
# looks worse than the decision that caused it. Worth naming, not correcting.
INSTALMENTS = "Оплата частинами"


@dataclass(frozen=True)
class Signal:
    code: str
    text: str


def signals(lines: list[dict], pace_result: Pace, recurring_list: list,
            biggest: dict | None, unknown_share: float) -> list[Signal]:
    """What is worth saying out loud. An empty list is a valid answer.

    `recurring_list` is accepted but no longer produces signals of its own: the
    subscriptions table already marks each entry new or stopped, and repeating
    every one of them here filled a third of the first live report with
    duplicates of lines two blocks above.
    """
    found = []

    if pace_result.burned_pct - pace_result.elapsed_pct > PACE_AHEAD_POINTS:
        found.append(Signal("pace_ahead", (
            f"Спалено {pace_result.burned_pct:.0f}% доходу, "
            f"минуло {pace_result.elapsed_pct:.0f}% циклу — "
            f"тримайтесь {pace_result.sustainable_daily:.0f} ₴/день")))

    for line in lines:
        if line["name"] == INSTALMENTS:
            found.append(Signal("instalments", (
                f"{INSTALMENTS} {line['amount']:.0f} ₴ — це частина покупки "
                f"з іншого місяця")))
            continue
        # The unknown bucket growing is not actionable: the useful advice is
        # "answer the bot", which the unknown_share signal already gives.
        if line["name"] == UNKNOWN or line["is_new"] or line["delta_pct"] is None:
            continue
        over = line["amount"] - line["avg"]
        if line["delta_pct"] > CATEGORY_RISE_PCT and over > CATEGORY_RISE_UAH:
            found.append(Signal("category_up", (
                f"{line['name']} +{line['delta_pct']:.0f}% "
                f"(+{over:,.0f} ₴) до норми").replace(",", " ")))

    if (biggest and biggest.get("median")
            and biggest["amount"] > BIG_PURCHASE_MULTIPLE * biggest["median"]
            and biggest["amount"] >= BIG_PURCHASE_MIN_UAH):
        merchant = " ".join(str(biggest["merchant"]).split())
        found.append(Signal("big_purchase", (
            f"Найбільша покупка {biggest['amount']:,.0f} ₴ — "
            f"{merchant}").replace(",", " ")))

    if unknown_share > UNKNOWN_SHARE_LIMIT:
        found.append(Signal("unknown_share", (
            f"{unknown_share * 100:.0f}% витрат без категорії — "
            f"звіт неточний, варто відповісти боту")))

    return found


def latest_balance(rows: list[dict]) -> dict | None:
    """The balance after the most recent transaction, or None.

    The only use the balance column is put to, and never in arithmetic. With
    several cards it jumps between them, so this is the balance of whichever
    card was used last - which is why it is always reported together with the
    transaction it followed rather than as "money you have".
    """
    with_balance = [r for r in rows if r.get("balance") is not None]
    if not with_balance:
        return None
    newest = max(with_balance, key=lambda r: r["date"])
    return {"amount": float(newest["balance"]), "at": newest["date"],
            "merchant": newest.get("merchant") or ""}


@dataclass(frozen=True)
class Report:
    period: str
    start: date
    end: date
    key: str
    totals: Totals
    categories: list
    baseline_ready: bool
    cycle: IncomeCycle
    pace: Pace
    recurring: list
    top_merchants: list
    balance: dict | None
    biggest: dict | None
    top_day: dict | None
    unknown_share: float
    tx_count: int
    signals: list


def build_report(rows: list[dict], period: str, today: date) -> Report:
    """Everything the digest needs, in one object.

    The same object is both formatted for Telegram and serialized for the
    model, so the two can never disagree about the numbers.
    """
    start, end = week_bounds(today) if period == "week" else month_bounds(today)
    window = rows_between(rows, start, end)
    figures = totals(window)

    actual = by_category(window)
    # Baseline is anchored on the REPORT's start, not on today: anchoring on
    # today would put the reported month inside its own norm and flatter every
    # monthly report.
    base = baseline(rows, start, weekly=(period == "week"))
    lines = category_lines(actual, base)

    consumption = [r for r in window if classify(r) == "spend"]
    amounts = [float(r["amount"]) for r in consumption]
    biggest = None
    if amounts:
        top = max(consumption, key=lambda r: float(r["amount"]))
        biggest = {"merchant": top["merchant"], "amount": float(top["amount"]),
                   "median": round(median(amounts), 2)}

    per_day: dict[date, float] = {}
    for item in consumption:
        day = item["date"].date()
        per_day[day] = round(per_day.get(day, 0.0) + float(item["amount"]), 2)
    top_day = None
    if per_day:
        day, amount = max(per_day.items(), key=lambda pair: pair[1])
        top_day = {"date": day, "amount": amount}

    unknown = sum(a for r, a in zip(consumption, amounts) if r["category"] == UNKNOWN)
    unknown_share = round(unknown / figures.spend, 4) if figures.spend else 0.0

    cycle = detect_income_cycle(rows, today)
    pace_result = pace(rows, cycle, today)
    # Monthly only. A monthly charge cannot appear inside a single week, so
    # evaluating subscription status against a week marks every one of them
    # "gone" - on live data that produced eight junk signals in one report.
    subs = recurring(rows, today, period_rows=window) if period == "month" else []

    return Report(
        period=period,
        start=start,
        end=end,
        key=period_key(period, start),
        totals=figures,
        categories=lines,
        baseline_ready=bool(base),
        cycle=cycle,
        pace=pace_result,
        recurring=subs,
        top_merchants=top_merchants(window),
        # From the whole sheet, not the window: the question it answers is
        # "how much is on the card now".
        balance=latest_balance(rows),
        biggest=biggest,
        top_day=top_day,
        unknown_share=unknown_share,
        tx_count=len(window),
        signals=signals(lines, pace_result, subs, biggest, unknown_share),
    )

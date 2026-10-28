from datetime import date, datetime

import pytest

import analytics


def row(kind="Витрата", category="Продукти", amount=100.0, merchant="Сільпо",
        date=datetime(2026, 7, 15)):
    return {"date": date, "kind": kind, "category": category,
            "merchant": merchant, "amount": amount}


def test_own_card_transfer_is_internal():
    assert analytics.classify(row(kind="Переказ", category="Перекази — свої картки")) == "internal"


def test_income_is_income():
    assert analytics.classify(row(kind="Дохід", category="Зарплата")) == "income"


def test_cash_withdrawal_is_not_consumption():
    assert analytics.classify(row(category="Зняття готівки")) == "cash"


def test_p2p_transfer_is_not_consumption():
    assert analytics.classify(row(category="Перекази — людям / P2P")) == "transfers_out"


def test_ordinary_expense_is_spend():
    assert analytics.classify(row()) == "spend"


def test_unknown_kind_counts_as_spend_not_as_income():
    # Historical rows may predate the three-value Type column. Counting an
    # unknown row as an outflow understates nothing; counting it as income
    # would silently inflate the pace denominator.
    assert analytics.classify(row(kind="")) == "spend"


def test_internal_transfers_are_excluded_from_every_total():
    rows = [
        row(kind="Переказ", category="Перекази — свої картки", amount=5000.0),
        row(kind="Дохід", category="Зарплата", amount=20000.0),
        row(amount=300.0),
        row(category="Зняття готівки", amount=1000.0),
        row(category="Перекази — людям / P2P", amount=500.0),
    ]
    t = analytics.totals(rows)
    assert t.income == 20000.0
    assert t.spend == 300.0
    assert t.cash == 1000.0
    assert t.transfers_out == 500.0
    assert t.total_outflow == 1800.0


def test_totals_of_nothing_are_zero_not_an_error():
    t = analytics.totals([])
    assert (t.income, t.spend, t.cash, t.transfers_out, t.total_outflow) == (0.0, 0.0, 0.0, 0.0, 0.0)


def test_week_bounds_return_the_completed_previous_week():
    # 2026-08-03 is a Monday; the completed week is 27 Jul - 2 Aug.
    assert analytics.week_bounds(date(2026, 8, 3)) == (date(2026, 7, 27), date(2026, 8, 2))


def test_week_bounds_work_midweek_too():
    # Friday 2026-08-07 still reports the same completed week.
    assert analytics.week_bounds(date(2026, 8, 7)) == (date(2026, 7, 27), date(2026, 8, 2))


def test_week_bounds_cross_the_new_year():
    assert analytics.week_bounds(date(2027, 1, 4)) == (date(2026, 12, 28), date(2027, 1, 3))


def test_month_bounds_return_the_previous_calendar_month():
    assert analytics.month_bounds(date(2026, 8, 1)) == (date(2026, 7, 1), date(2026, 7, 31))


def test_month_bounds_handle_february_in_a_leap_year():
    assert analytics.month_bounds(date(2028, 3, 1)) == (date(2028, 2, 1), date(2028, 2, 29))


def test_month_bounds_cross_the_new_year():
    assert analytics.month_bounds(date(2027, 1, 1)) == (date(2026, 12, 1), date(2026, 12, 31))


@pytest.mark.parametrize("period,start,expected", [
    ("week", date(2026, 7, 27), "2026-W31"),
    ("month", date(2026, 7, 1), "2026-07"),
])
def test_period_key(period, start, expected):
    assert analytics.period_key(period, start) == expected


def test_rows_between_is_inclusive_on_both_ends():
    rows = [row(date=datetime(2026, 7, 26, 23, 59)),
            row(date=datetime(2026, 7, 27, 0, 0)),
            row(date=datetime(2026, 8, 2, 23, 59)),
            row(date=datetime(2026, 8, 3, 0, 1))]
    kept = analytics.rows_between(rows, date(2026, 7, 27), date(2026, 8, 2))
    assert [r["date"].day for r in kept] == [27, 2]


def test_by_category_counts_only_consumption():
    rows = [row(category="Продукти", amount=100.0),
            row(category="Продукти", amount=50.0),
            row(category="Зняття готівки", amount=900.0),
            row(kind="Дохід", category="Зарплата", amount=20000.0)]
    assert analytics.by_category(rows) == {"Продукти": 150.0}


def test_baseline_window_is_the_three_complete_months_before_this_one():
    assert analytics.baseline_window(date(2026, 8, 7)) == (date(2026, 5, 1), date(2026, 7, 31))


def test_baseline_window_crosses_the_new_year():
    assert analytics.baseline_window(date(2027, 1, 15)) == (date(2026, 10, 1), date(2026, 12, 31))


def test_monthly_baseline_is_the_average_per_month():
    rows = [row(date=datetime(2026, 5, 10), amount=300.0),
            row(date=datetime(2026, 6, 10), amount=300.0),
            row(date=datetime(2026, 7, 10), amount=300.0)]
    assert analytics.baseline(rows, date(2026, 8, 7))["Продукти"] == {"avg": 300.0, "seen": 3}


def test_weekly_baseline_divides_the_monthly_one_by_the_weeks_in_a_month():
    rows = [row(date=datetime(2026, 5, 10), amount=433.0),
            row(date=datetime(2026, 6, 10), amount=433.0),
            row(date=datetime(2026, 7, 10), amount=433.0)]
    assert analytics.baseline(rows, date(2026, 8, 7), weekly=True)["Продукти"]["avg"] == 100.0


def test_baseline_ignores_rows_outside_the_window():
    rows = [row(date=datetime(2026, 4, 10), amount=9999.0),   # too old
            row(date=datetime(2026, 8, 5), amount=9999.0),    # this month
            row(date=datetime(2026, 6, 10), amount=300.0)]
    assert analytics.baseline(rows, date(2026, 8, 7))["Продукти"] == {"avg": 100.0, "seen": 1}


def test_a_category_seen_once_is_new_and_carries_no_percentage():
    base = {"Продукти": {"avg": 300.0, "seen": 5}, "Квіти": {"avg": 200.0, "seen": 1}}
    lines = analytics.category_lines({"Продукти": 330.0, "Квіти": 400.0, "Спорт": 500.0}, base)
    by_name = {line["name"]: line for line in lines}
    assert by_name["Продукти"]["delta_pct"] == 10.0
    assert by_name["Продукти"]["is_new"] is False
    # seen once: a baseline of one observation is noise, not a norm
    assert by_name["Квіти"]["is_new"] is True
    assert by_name["Квіти"]["delta_pct"] is None
    # absent from the baseline entirely
    assert by_name["Спорт"]["is_new"] is True
    assert by_name["Спорт"]["delta_pct"] is None


def test_category_lines_are_sorted_by_amount_descending():
    lines = analytics.category_lines({"A": 10.0, "B": 500.0, "C": 100.0}, {})
    assert [line["name"] for line in lines] == ["B", "C", "A"]


def income(day, month, amount=20000.0, year=2026):
    return row(kind="Дохід", category="Зарплата", merchant="ТОВ",
               amount=amount, date=datetime(year, month, day))


def test_a_steady_monthly_salary_is_detected():
    rows = [income(10, m) for m in (3, 4, 5, 6, 7)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 8, 7))
    assert cycle.kind == "monthly"
    assert cycle.anchor_days == (10,)
    assert cycle.expected == 20000.0
    assert cycle.next_at == date(2026, 8, 10)


def test_a_salary_a_day_or_two_adrift_still_counts_as_one_cluster():
    rows = [income(9, 4), income(11, 5), income(10, 6), income(12, 7)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 8, 7))
    assert cycle.kind == "monthly"


def test_an_advance_plus_salary_is_semimonthly():
    rows = []
    for m in (4, 5, 6, 7):
        rows += [income(5, m, amount=8000.0), income(20, m, amount=12000.0)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 8, 7))
    assert cycle.kind == "semimonthly"
    assert cycle.anchor_days == (5, 20)
    assert cycle.next_at == date(2026, 8, 20)


def test_small_credits_do_not_create_a_phantom_cycle():
    # A 50 UAH refund on the 2nd of every month must not look like income.
    rows = [income(10, m) for m in (4, 5, 6, 7)]
    rows += [income(2, m, amount=50.0) for m in (4, 5, 6, 7)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 8, 7))
    assert cycle.kind == "monthly"
    assert cycle.anchor_days == (10,)


def test_scattered_income_is_irregular_but_still_reports_an_expected_amount():
    rows = [income(3, 4), income(19, 5), income(27, 6), income(8, 7)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 8, 7))
    assert cycle.kind == "irregular"
    assert cycle.anchor_days == ()
    assert cycle.expected == 20000.0
    assert cycle.next_at is None


def test_no_income_at_all_is_irregular_and_expects_nothing():
    cycle = analytics.detect_income_cycle([row()], date(2026, 8, 7))
    assert cycle == analytics.IncomeCycle("irregular", (), 0.0, None)


def test_a_single_month_of_history_is_not_enough_to_claim_a_cycle():
    cycle = analytics.detect_income_cycle([income(10, 7)], date(2026, 8, 7))
    assert cycle.kind == "irregular"


def test_the_anchor_is_clamped_to_a_short_month():
    rows = [income(31, m) for m in (3, 5, 7)] + [income(30, m) for m in (4, 6)]
    cycle = analytics.detect_income_cycle(rows, date(2026, 9, 15))
    # anchor lands on the 30th or 31st; September has 30 days
    assert cycle.next_at == date(2026, 9, 30)


def spend(day, month, amount, year=2026):
    return row(amount=amount, date=datetime(year, month, day))


def test_pace_measures_the_cycle_not_the_report_period():
    cycle = analytics.IncomeCycle("monthly", (10,), 20000.0, date(2026, 8, 10))
    rows = [income(10, 7), spend(15, 7, 5000.0)]
    p = analytics.pace(rows, cycle, date(2026, 7, 20))
    assert p.start == date(2026, 7, 10)
    assert p.end == date(2026, 8, 10)
    assert p.income == 20000.0
    assert p.outflow == 5000.0
    assert p.burned_pct == 25.0


def test_pace_flags_running_ahead_of_the_cycle():
    cycle = analytics.IncomeCycle("monthly", (10,), 20000.0, date(2026, 8, 10))
    rows = [income(10, 7), spend(12, 7, 10400.0)]
    p = analytics.pace(rows, cycle, date(2026, 7, 18))
    # 9 of 31 days gone, 52% of income spent
    assert round(p.elapsed_pct) == 29
    assert p.burned_pct == 52.0
    assert p.days_left == 23
    assert p.runway_gap < 0            # money runs out before the next payment


def test_sustainable_daily_is_what_is_left_divided_by_days_left():
    cycle = analytics.IncomeCycle("monthly", (10,), 20000.0, date(2026, 8, 10))
    rows = [income(10, 7), spend(12, 7, 10000.0)]
    p = analytics.pace(rows, cycle, date(2026, 7, 20))
    assert p.days_left == 21
    assert p.sustainable_daily == round(10000.0 / 21, 2)


def test_income_not_yet_received_falls_back_to_the_expected_amount():
    cycle = analytics.IncomeCycle("monthly", (10,), 20000.0, date(2026, 8, 10))
    rows = [spend(12, 7, 4000.0)]
    p = analytics.pace(rows, cycle, date(2026, 7, 20))
    assert p.income_received is False
    assert p.expected == 20000.0
    assert p.burned_pct == 20.0


def test_an_irregular_cycle_falls_back_to_the_calendar_month():
    cycle = analytics.IncomeCycle("irregular", (), 18000.0, None)
    rows = [spend(5, 7, 9000.0)]
    p = analytics.pace(rows, cycle, date(2026, 7, 20))
    assert p.start == date(2026, 7, 1)
    assert p.end == date(2026, 8, 1)
    assert p.burned_pct == 50.0


def test_zero_expected_income_does_not_divide_by_zero():
    cycle = analytics.IncomeCycle("irregular", (), 0.0, None)
    p = analytics.pace([spend(5, 7, 900.0)], cycle, date(2026, 7, 20))
    assert p.burned_pct == 0.0
    assert p.expected == 0.0


def test_spending_nothing_gives_no_runway_shortfall():
    cycle = analytics.IncomeCycle("monthly", (10,), 20000.0, date(2026, 8, 10))
    p = analytics.pace([income(10, 7)], cycle, date(2026, 7, 20))
    assert p.current_daily == 0.0
    assert p.runway_gap >= 0


def sub(merchant, day, month, amount, year=2026):
    return row(category="Підписки / Зв'язок", merchant=merchant,
               amount=amount, date=datetime(year, month, day))


def test_a_charge_in_three_of_four_months_is_recurring():
    rows = [sub("Netflix", 5, m, 270.0) for m in (4, 5, 6, 7)]
    found = analytics.recurring(rows, date(2026, 8, 7), period_rows=rows[-1:])
    assert found[0].merchant == "Netflix"
    assert found[0].amount == 270.0
    assert found[0].months == 4
    assert found[0].status == "active"


def test_a_wandering_amount_is_not_a_subscription():
    rows = [sub("Сільпо", 5, 4, 200.0), sub("Сільпо", 6, 5, 900.0),
            sub("Сільпо", 7, 6, 350.0), sub("Сільпо", 8, 7, 1400.0)]
    assert analytics.recurring(rows, date(2026, 8, 7), period_rows=[]) == []


def test_fifteen_percent_drift_is_still_the_same_subscription():
    rows = [sub("Kyivstar", 5, 4, 250.0), sub("Kyivstar", 5, 5, 262.0),
            sub("Kyivstar", 5, 6, 240.0), sub("Kyivstar", 5, 7, 255.0)]
    assert analytics.recurring(rows, date(2026, 8, 7), period_rows=rows[-1:])[0].months == 4


def test_two_months_running_is_reported_as_new():
    rows = [sub("Spotify", 3, 6, 165.0), sub("Spotify", 3, 7, 165.0)]
    found = analytics.recurring(rows, date(2026, 8, 7), period_rows=rows[-1:])
    assert [(f.merchant, f.status) for f in found] == [("Spotify", "new")]


def test_an_established_charge_missing_from_the_period_is_gone():
    rows = [sub("Спортзал", 2, m, 1200.0) for m in (4, 5, 6, 7)]
    found = analytics.recurring(rows, date(2026, 8, 7), period_rows=[])
    assert found[0].status == "gone"


def test_recurring_ignores_income_and_cash():
    rows = [income(10, m) for m in (4, 5, 6, 7)]
    rows += [row(category="Зняття готівки", merchant="ATM", amount=1000.0,
                 date=datetime(2026, m, 3)) for m in (4, 5, 6, 7)]
    assert analytics.recurring(rows, date(2026, 8, 7), period_rows=[]) == []


def test_results_are_sorted_by_amount_descending():
    rows = [sub("Netflix", 5, m, 270.0) for m in (4, 5, 6, 7)]
    rows += [sub("Спортзал", 6, m, 1200.0) for m in (4, 5, 6, 7)]
    found = analytics.recurring(rows, date(2026, 8, 7), period_rows=rows)
    assert [f.merchant for f in found] == ["Спортзал", "Netflix"]


def codes(found):
    return [s.code for s in found]


QUIET_PACE = analytics.Pace(
    start=date(2026, 7, 1), end=date(2026, 8, 1), elapsed_pct=50.0,
    burned_pct=48.0, days_left=15, income=20000.0, expected=20000.0,
    outflow=9600.0, current_daily=640.0, sustainable_daily=693.0,
    runway_gap=1, income_received=True)


def test_a_big_relative_rise_on_a_tiny_sum_is_not_a_signal():
    lines = [{"name": "Квіти", "amount": 72.0, "avg": 40.0,
              "delta_pct": 80.0, "is_new": False}]
    assert analytics.signals(lines, QUIET_PACE, [], None, 0.0) == []


def test_a_modest_rise_on_a_large_sum_is_a_signal():
    lines = [{"name": "Кафе", "amount": 3400.0, "avg": 2600.0,
              "delta_pct": 30.8, "is_new": False}]
    assert codes(analytics.signals(lines, QUIET_PACE, [], None, 0.0)) == ["category_up"]


def test_pace_running_well_ahead_of_the_cycle_is_a_signal():
    hot = analytics.Pace(
        start=date(2026, 7, 1), end=date(2026, 8, 1), elapsed_pct=27.0,
        burned_pct=52.0, days_left=16, income=20000.0, expected=20000.0,
        outflow=10400.0, current_daily=980.0, sustainable_daily=600.0,
        runway_gap=-4, income_received=True)
    assert codes(analytics.signals([], hot, [], None, 0.0)) == ["pace_ahead"]


def test_subscription_changes_do_not_become_signals():
    # The subscriptions table marks each entry "нове" or "стоп" already.
    # Repeating them here filled a third of the first live report with
    # duplicates of lines two blocks above.
    subs = [analytics.Recurring("Spotify", 165.0, 2, "new"),
            analytics.Recurring("Спортзал", 1200.0, 4, "gone")]
    assert analytics.signals([], QUIET_PACE, subs, None, 0.0) == []


def test_an_outsized_single_purchase_is_a_signal():
    biggest = {"merchant": "Rozetka", "amount": 8400.0, "median": 300.0}
    assert codes(analytics.signals([], QUIET_PACE, [], biggest, 0.0)) == ["big_purchase"]


def test_a_purchase_within_three_times_the_median_is_not():
    biggest = {"merchant": "Сільпо", "amount": 800.0, "median": 300.0}
    assert analytics.signals([], QUIET_PACE, [], biggest, 0.0) == []


def test_too_many_uncategorized_rows_is_a_signal():
    assert codes(analytics.signals([], QUIET_PACE, [], None, 0.14)) == ["unknown_share"]


def test_instalments_are_called_out_because_they_smear_one_purchase():
    lines = [{"name": "Оплата частинами", "amount": 1200.0, "avg": None,
              "delta_pct": None, "is_new": True}]
    assert codes(analytics.signals(lines, QUIET_PACE, [], None, 0.0)) == ["instalments"]


def test_an_empty_signal_list_is_a_valid_result():
    assert analytics.signals([], QUIET_PACE, [], None, 0.02) == []


def test_build_report_covers_the_completed_week():
    rows = [spend(28, 7, 200.0), spend(3, 8, 999.0)]   # 3 Aug is outside
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert (report.start, report.end) == (date(2026, 7, 27), date(2026, 8, 2))
    assert report.key == "2026-W31"
    assert report.totals.spend == 200.0
    assert report.tx_count == 1


def test_build_report_covers_the_previous_month():
    rows = [spend(15, 7, 200.0), spend(2, 8, 999.0)]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    assert (report.start, report.end) == (date(2026, 7, 1), date(2026, 7, 31))
    assert report.key == "2026-07"
    assert report.totals.spend == 200.0


def test_baseline_ready_is_false_without_enough_history():
    report = analytics.build_report([spend(15, 7, 200.0)], "month", date(2026, 8, 1))
    assert report.baseline_ready is False


def test_baseline_ready_is_true_with_three_months_of_history():
    rows = [spend(10, m, 300.0) for m in (5, 6, 7)] + [spend(10, 8, 400.0)]
    report = analytics.build_report(rows, "month", date(2026, 9, 1))
    assert report.baseline_ready is True


def test_the_reported_month_is_not_part_of_its_own_baseline():
    # July is the reported month; its norm must come from April-June only.
    rows = [spend(10, m, 300.0) for m in (4, 5, 6)] + [spend(10, 7, 600.0)]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    line = report.categories[0]
    assert line["avg"] == 300.0
    assert line["delta_pct"] == 100.0


def test_unknown_share_counts_only_consumption():
    rows = [spend(28, 7, 300.0),
            row(category="Не визначено", amount=100.0, date=datetime(2026, 7, 28))]
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert report.unknown_share == 0.25


def test_top_day_is_the_most_expensive_day_of_the_period():
    rows = [spend(28, 7, 300.0), spend(30, 7, 1200.0), spend(30, 7, 100.0)]
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert report.top_day == {"date": date(2026, 7, 30), "amount": 1300.0}


def test_an_empty_period_produces_a_report_rather_than_an_error():
    report = analytics.build_report([], "week", date(2026, 8, 3))
    assert report.tx_count == 0
    assert report.categories == []
    assert report.biggest is None
    assert report.top_day is None


def test_a_weekly_report_never_reports_subscriptions():
    # A monthly charge cannot appear inside a single week, so evaluating
    # subscription status against a week marks every one of them "gone" - on
    # live data that produced eight junk signals in one report.
    rows = [sub("Netflix", 5, m, 270.0) for m in (4, 5, 6, 7)]
    rows.append(spend(28, 7, 100.0))
    weekly = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert weekly.recurring == []
    assert [s.code for s in weekly.signals if "recurring" in s.code] == []
    monthly = analytics.build_report(rows, "month", date(2026, 8, 1))
    assert monthly.recurring


def test_a_trivially_small_repeat_charge_is_not_a_subscription():
    # An 8 UAH metro fare repeats every month. It is a habit, not a
    # subscription, and listing it drowns the ones that matter.
    rows = [sub("METROPOLITEN", 5, m, 8.0) for m in (4, 5, 6, 7)]
    assert analytics.recurring(rows, date(2026, 8, 7), period_rows=rows) == []


def test_a_big_purchase_needs_an_absolute_floor_too():
    # 129 UAH is three times the median of a quiet week, and reporting it as
    # the standout purchase is noise.
    small = {"merchant": "Сільпо", "amount": 129.0, "median": 30.0}
    assert analytics.signals([], QUIET_PACE, [], small, 0.0) == []


def test_the_unknown_bucket_growing_is_not_reported_as_a_category_rise():
    # "Не визначено grew 57%" is not actionable - the actionable thing is
    # "answer the bot", which the unknown_share signal already says.
    lines = [{"name": "Не визначено", "amount": 11849.0, "avg": 7548.0,
              "delta_pct": 57.0, "is_new": False}]
    assert codes(analytics.signals(lines, QUIET_PACE, [], None, 0.26)) == ["unknown_share"]


def test_top_merchants_sums_per_merchant_and_sorts_by_amount():
    rows = [row(merchant="Glovo", category="Кафе та ресторани", amount=900.0),
            row(merchant="Glovo", category="Кафе та ресторани", amount=900.0),
            row(merchant="Сільпо", category="Продукти", amount=1000.0)]
    assert analytics.top_merchants(rows) == [
        {"name": "Glovo", "amount": 1800.0, "category": "Кафе та ресторани"},
        {"name": "Сільпо", "amount": 1000.0, "category": "Продукти"}]


def test_top_merchants_ignores_everything_that_is_not_consumption():
    rows = [row(kind="Дохід", category="Зарплата", merchant="ТОВ", amount=20000.0),
            row(category="Зняття готівки", merchant="ATM", amount=5000.0),
            row(category="Перекази — людям / P2P", merchant="Іваненко І.", amount=800.0),
            row(merchant="Сільпо", amount=100.0)]
    assert [m["name"] for m in analytics.top_merchants(rows)] == ["Сільпо"]


def test_top_merchants_respects_the_limit():
    rows = [row(merchant=f"M{i}", amount=float(i)) for i in range(20)]
    assert len(analytics.top_merchants(rows, limit=5)) == 5


def test_top_merchants_skips_blank_names():
    rows = [row(merchant="", amount=500.0), row(merchant="Сільпо", amount=100.0)]
    assert [m["name"] for m in analytics.top_merchants(rows)] == ["Сільпо"]


def test_the_report_carries_top_merchants_for_its_own_window():
    rows = [spend(28, 7, 300.0), spend(3, 8, 999.0)]   # 3 Aug is outside the week
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert [m["amount"] for m in report.top_merchants] == [300.0]


def test_latest_balance_is_the_newest_row_that_has_one():
    rows = [dict(row(date=datetime(2026, 7, 1)), balance=100.0),
            dict(row(date=datetime(2026, 7, 20), merchant="Сільпо"), balance=555.5),
            dict(row(date=datetime(2026, 7, 10)), balance=300.0)]
    got = analytics.latest_balance(rows)
    assert got == {"amount": 555.5, "at": datetime(2026, 7, 20), "merchant": "Сільпо"}


def test_latest_balance_ignores_rows_without_one():
    rows = [dict(row(date=datetime(2026, 7, 1)), balance=100.0),
            dict(row(date=datetime(2026, 7, 20)), balance=None)]
    assert analytics.latest_balance(rows)["amount"] == 100.0


def test_latest_balance_is_none_when_nothing_has_one():
    assert analytics.latest_balance([dict(row(), balance=None)]) is None


def test_latest_balance_is_none_for_an_empty_sheet():
    assert analytics.latest_balance([]) is None


def test_the_report_carries_the_latest_balance_from_the_whole_sheet():
    # Deliberately the newest row overall, not the newest in the period: the
    # question it answers is "how much is on the card now".
    rows = [dict(spend(28, 7, 300.0), balance=900.0),
            dict(spend(5, 8, 100.0), balance=800.0)]
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    assert report.balance["amount"] == 800.0

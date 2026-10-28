import json

import pytest

import digest

DEFAULTS = {"last_weekly": "", "last_monthly": "", "model": ""}


@pytest.mark.parametrize("text", [None, "", "   ", "{not json", "42", "null",
                                  "[1, 2]", '"a string"'])
def test_unusable_state_degrades_to_defaults(text):
    assert digest.parse_state(text) == DEFAULTS


def test_state_round_trip():
    state = {"last_weekly": "2026-W31", "last_monthly": "2026-07",
             "model": "google/gemma-4-31b-it:free"}
    assert digest.parse_state(digest.serialize_state(state)) == state


def test_partial_state_is_filled_with_defaults():
    assert digest.parse_state(json.dumps({"last_weekly": "2026-W31"})) == {
        "last_weekly": "2026-W31", "last_monthly": "", "model": ""}


def test_non_string_values_are_coerced_not_trusted():
    assert digest.parse_state(json.dumps({"last_weekly": 31})) == {
        "last_weekly": "31", "last_monthly": "", "model": ""}


def test_serialize_drops_keys_that_are_not_persisted():
    text = digest.serialize_state({"last_weekly": "2026-W31", "sent_at": "now"})
    assert json.loads(text) == {"last_weekly": "2026-W31", "last_monthly": "", "model": ""}


from datetime import date, datetime

import analytics


def spend_row(day, month, amount, merchant="Сільпо", category="Продукти", year=2026):
    return {"date": datetime(year, month, day), "kind": "Витрата",
            "category": category, "merchant": merchant, "amount": amount}


def sample_report(period="week", rows=None, today=date(2026, 8, 3)):
    rows = rows if rows is not None else [spend_row(28, 7, 250.0)]
    return analytics.build_report(rows, period, today)


def test_report_text_names_the_period_and_the_total():
    text = digest.format_report(sample_report())
    assert "27" in text and "2" in text
    assert "250" in text


def test_a_merchant_with_html_characters_cannot_break_the_message():
    # Three rows so the median is small and the big-purchase signal fires -
    # that signal is what puts a merchant name into the text at all.
    rows = [spend_row(28, 7, 100.0), spend_row(29, 7, 100.0),
            spend_row(30, 7, 5000.0, merchant="Rozetka & <Co>")]
    text = digest.format_report(sample_report(rows=rows))
    assert "&amp;" in text
    assert "<Co>" not in text


def test_the_message_fits_telegrams_limit():
    rows = [spend_row(28, 7, 100.0 + i, merchant=f"Мерчант {i}",
                      category=f"Категорія {i}") for i in range(200)]
    text = digest.format_report(sample_report(rows=rows))
    assert len(text) <= digest.MAX_MESSAGE


def test_an_empty_period_says_so_rather_than_printing_an_empty_table():
    text = digest.format_report(sample_report(rows=[]))
    assert "транзакцій не було" in text


def test_a_missing_baseline_is_stated_not_hidden():
    text = digest.format_report(sample_report())
    assert "норма ще накопичується" in text


def test_the_missing_analysis_is_announced():
    text = digest.format_report(sample_report(), llm_available=False)
    assert "Аналіз від моделі ще не підключено" in text


def test_income_not_yet_received_is_labelled_as_expected_not_as_fact():
    # Salary on the 10th of April, May and June: enough to detect the cycle,
    # but the cycle running on 3 Aug (10 Jul - 10 Aug) contains no credit.
    rows = [spend_row(28, 7, 250.0)]
    rows += [{"date": datetime(2026, m, 10), "kind": "Дохід", "category": "Зарплата",
              "merchant": "ТОВ", "amount": 20000.0} for m in (4, 5, 6)]
    text = digest.format_report(analytics.build_report(rows, "week", date(2026, 8, 3)))
    assert "очікується" in text.lower()


def test_truncation_never_cuts_an_html_entity_in_half():
    # Escaping before truncating turns "Зв'язок" into "Зв&#x27;язок" and the
    # cut can land inside "&#x27;", which is malformed HTML - Telegram rejects
    # the whole message. A long name with an apostrophe near the limit is the
    # trigger; the category column truncates at 20 characters.
    rows = [spend_row(28, 7, 250.0, category="Підписки і зв'язок вдома")]
    text = digest.format_report(sample_report(rows=rows))
    assert "&#x2" not in text.replace("&#x27;", "")
    assert "&#" not in text.replace("&#x27;", "")


class FakeClient:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send_message(self, chat_id, text, keyboard=None, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))
        return {"message_id": 1} if self.ok else None


class FakeSheets:
    DIGEST_STATE_RANGE = "_state!A2"

    def __init__(self, rows, state_text=None):
        self._rows = rows
        self.state_text = state_text
        self.writes = []

    def all_rows(self):
        return self._rows

    def read_state(self, cell=None):
        return self.state_text

    def write_state(self, text, cell=None):
        self.writes.append((cell, text))
        self.state_text = text


def test_monday_is_due_for_the_week():
    assert digest.due_periods(date(2026, 8, 3), DEFAULTS) == ["week"]


def test_the_first_of_the_month_is_due_for_the_month():
    assert digest.due_periods(date(2026, 9, 1), DEFAULTS) == ["month"]


def test_a_monday_on_the_first_is_due_for_both():
    assert digest.due_periods(date(2026, 6, 1), DEFAULTS) == ["week", "month"]


def test_an_ordinary_day_is_due_for_nothing():
    assert digest.due_periods(date(2026, 8, 5), DEFAULTS) == []


def test_an_already_sent_period_is_not_due_again():
    state = dict(DEFAULTS, last_weekly="2026-W31")
    assert digest.due_periods(date(2026, 8, 3), state) == []


def test_state_is_written_only_after_a_confirmed_send(monkeypatch):
    monkeypatch.setattr(digest, "TelegramClient", lambda token: FakeClient())
    sheets = FakeSheets([spend_row(28, 7, 250.0)])
    digest.run(sheets, "token", 42, today=date(2026, 8, 3))
    assert sheets.writes
    assert digest.parse_state(sheets.state_text)["last_weekly"] == "2026-W31"


def test_a_rejected_message_leaves_the_state_untouched(monkeypatch):
    sheets = FakeSheets([spend_row(28, 7, 250.0)])
    monkeypatch.setattr(digest, "TelegramClient", lambda token: FakeClient(ok=False))
    digest.run(sheets, "token", 42, today=date(2026, 8, 3))
    assert sheets.writes == []


def test_a_second_run_on_the_same_day_sends_nothing(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(digest, "TelegramClient", lambda token: client)
    sheets = FakeSheets([spend_row(28, 7, 250.0)])
    digest.run(sheets, "token", 42, today=date(2026, 8, 3))
    digest.run(sheets, "token", 42, today=date(2026, 8, 3))
    assert len(client.sent) == 1


def test_the_message_is_sent_as_html(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(digest, "TelegramClient", lambda token: client)
    digest.run(FakeSheets([spend_row(28, 7, 250.0)]), "token", 42,
               today=date(2026, 8, 3))
    assert client.sent[0][2] == "HTML"


def test_no_chat_id_means_nothing_is_sent_and_nothing_crashes(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(digest, "TelegramClient", lambda token: client)
    sheets = FakeSheets([spend_row(28, 7, 250.0)])
    digest.run(sheets, "token", None, today=date(2026, 8, 3))
    assert client.sent == []
    assert sheets.writes == []


def test_both_reports_on_the_same_day_are_two_separate_messages(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(digest, "TelegramClient", lambda token: client)
    sheets = FakeSheets([spend_row(28, 5, 250.0)])
    digest.run(sheets, "token", 42, today=date(2026, 6, 1))
    assert len(client.sent) == 2
    state = digest.parse_state(sheets.state_text)
    assert state["last_weekly"] and state["last_monthly"]


def test_merchant_noise_cannot_become_a_clickable_link():
    # Telegram linkifies "24.24.24.24" as an IP. Inside <pre> it does not, and
    # every name+amount pair now lives in a <pre> block.
    rows = [{"date": datetime(2026, m, 5), "kind": "Витрата",
             "category": "Послуги", "merchant": "SALONBELLA  24.24.24.24.24 AVE",
             "amount": 418.0} for m in (4, 5, 6, 7)]
    text = digest.format_report(analytics.build_report(rows, "month", date(2026, 8, 1)))
    assert "24.24" in text
    # the merchant never appears outside a code entity, where Telegram would
    # turn it into a clickable link
    outside = "".join(part.split("<pre>")[0] for part in text.split("</pre>"))
    assert "24.24" not in outside


def test_a_long_merchant_is_collapsed_and_kept_apart_from_its_amount():
    rows = [{"date": datetime(2026, m, 5), "kind": "Витрата",
             "category": "Кредит", "merchant": "Кредит готівкою 12345678901234",
             "amount": 1432.0} for m in (4, 5, 6, 7)]
    text = digest.format_report(analytics.build_report(rows, "month", date(2026, 8, 1)))
    line = next(ln for ln in text.splitlines() if "Кредит готівкою" in ln)
    # at least two spaces between the name and the number, so they never merge
    assert "  " in line.rstrip()


def test_new_subscriptions_are_not_repeated_in_the_signal_list():
    # The subscriptions block already marks them; repeating each one as a
    # signal filled a third of the live report with duplicates.
    rows = [{"date": datetime(2026, m, 5), "kind": "Витрата", "category": "Розваги",
             "merchant": "Spotify", "amount": 165.0} for m in (6, 7)]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    assert [s.code for s in report.signals if "recurring" in s.code] == []


def test_tables_are_pre_so_telegram_frames_each_section():
    # The frame Telegram draws around a <pre> block is what separates one
    # section from the next; <code> renders flat and the sections ran together.
    text = digest.format_report(sample_report())
    assert "<pre>" in text


def test_a_weekly_report_does_not_claim_the_week_burned_the_cycle():
    # The pace block is cycle-scoped, the totals are week-scoped. Side by side
    # ("336 ₴ spent" / "74% burned") they read as a contradiction unless the
    # cycle is named.
    rows = [spend_row(28, 7, 250.0)]
    rows += [{"date": datetime(2026, m, 10), "kind": "Дохід", "category": "Зарплата",
              "merchant": "ТОВ", "amount": 20000.0} for m in (4, 5, 6)]
    text = digest.format_report(analytics.build_report(rows, "week", date(2026, 8, 3)))
    assert "Цикл " in text


def test_a_quiet_period_says_so_instead_of_dropping_the_section():
    text = digest.format_report(sample_report())
    assert "не вибивається" in text


def test_the_most_expensive_day_is_a_monthly_line_only():
    # On a 336 UAH week, "priciest day - 129 UAH" is noise.
    text = digest.format_report(sample_report())
    assert "Найдорожчий день" not in text


def _table_rows(text, heading):
    """The lines of the <pre> block that follows a given heading."""
    import html as h
    after = text.split(heading, 1)[1]
    block = after.split("<pre>", 1)[1].split("</pre>", 1)[0]
    return h.unescape(block).splitlines()


def _numbers(lines):
    out = []
    for line in lines:
        digits = "".join(ch for ch in line if ch.isdigit() or ch == " ")
        parts = [p for p in digits.split("  ") if p.strip()]
        if parts:
            out.append(float(parts[-1].replace(" ", "")))
    return out


def test_the_category_table_accounts_for_every_hryvnia_of_spend():
    # Live July had 18 categories; showing 12 dropped 1 758 UAH with no trace,
    # so the column did not add up to the total above it.
    rows = [spend_row(10, 7, 1000.0 - i * 10, category=f"Кат {i:02d}")
            for i in range(18)]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    text = digest.format_report(report)
    shown = sum(_numbers(_table_rows(text, "Куди пішло")))
    assert round(shown, 2) == round(report.totals.spend, 2)
    assert "Решта" in text


def test_a_short_category_list_gets_no_residual_row():
    text = digest.format_report(sample_report())
    assert "Решта" not in text


def test_the_summary_reconciles_spend_movements_and_income():
    rows = [spend_row(10, 7, 1000.0),
            {"date": datetime(2026, 7, 11), "kind": "Витрата",
             "category": "Зняття готівки", "merchant": "ATM", "amount": 500.0},
            {"date": datetime(2026, 7, 12), "kind": "Дохід", "category": "Зарплата",
             "merchant": "ТОВ", "amount": 4000.0}]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    text = digest.format_report(report)
    assert "Витрати" in text and "Переміщення" in text and "Різниця" in text


def test_the_cycle_block_shows_the_numbers_its_percentages_come_from():
    # "74% of income" was unverifiable: neither 46 257 nor 62 923 appeared
    # anywhere in the report.
    rows = [spend_row(28, 7, 250.0)]
    rows += [{"date": datetime(2026, m, 10), "kind": "Дохід", "category": "Зарплата",
              "merchant": "ТОВ", "amount": 20000.0} for m in (4, 5, 6, 7)]
    report = analytics.build_report(rows, "week", date(2026, 8, 3))
    text = digest.format_report(report)
    assert digest._money(report.pace.outflow) in text
    assert digest._money(report.pace.expected) in text


def test_the_visible_column_adds_up_not_just_the_underlying_floats():
    # Rounding each of twelve lines to whole hryvnia left the printed column
    # one off the printed total. The residual row absorbs it by construction.
    rows = [spend_row(10, 7, 1000.0 + i * 0.5, category=f"Кат {i:02d}")
            for i in range(18)]
    report = analytics.build_report(rows, "month", date(2026, 8, 1))
    text = digest.format_report(report)
    printed = sum(_numbers(_table_rows(text, "Куди пішло")))
    assert printed == round(report.totals.spend)


def test_the_summary_block_subtracts_correctly_on_screen():
    # Надійшло − Витрати − Переміщення must equal Різниця using the printed
    # figures. Rounding each from its own float left the visible sum one out.
    rows = [spend_row(10, 7, 1000.4),
            {"date": datetime(2026, 7, 11), "kind": "Витрата",
             "category": "Зняття готівки", "merchant": "ATM", "amount": 500.4},
            {"date": datetime(2026, 7, 12), "kind": "Дохід", "category": "Зарплата",
             "merchant": "ТОВ", "amount": 4000.4}]
    text = digest.format_report(analytics.build_report(rows, "month", date(2026, 8, 1)))
    got = _numbers(_table_rows(text, "📈"))
    income, spend, moved, diff = got[0], got[1], got[2], got[3]
    assert income - spend - moved == diff


def test_payload_carries_the_figures_the_message_shows():
    report = sample_report()
    payload = digest.build_payload(report)
    assert payload["period"] == "week"
    assert payload["spend"] == round(report.totals.spend)
    assert payload["income"] == round(report.totals.income)
    assert payload["cycle"]["burned_pct"] == report.pace.burned_pct


def test_payload_strips_card_last_four_from_merchant_names():
    # apply_override depends on the "*NNNN" form the bank writes into transfer
    # text; it must not travel to a third party.
    rows = [{"date": datetime(2026, 7, 28), "kind": "Витрата", "category": "Послуги",
             "merchant": "Переказ на картку *3333", "amount": 900.0}]
    payload = digest.build_payload(analytics.build_report(rows, "week", date(2026, 8, 3)))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "3333" not in blob
    assert "Переказ на картку" in blob


def test_payload_collapses_the_padding_the_bank_adds():
    rows = [{"date": datetime(2026, 7, 28), "kind": "Витрата", "category": "Послуги",
             "merchant": "PORTMONE  INSURANCE   W  KYIV", "amount": 900.0}]
    payload = digest.build_payload(analytics.build_report(rows, "week", date(2026, 8, 3)))
    assert "PORTMONE INSURANCE W KYIV" in json.dumps(payload, ensure_ascii=False)


def test_payload_never_carries_transaction_ids_dates_or_balances():
    report = sample_report()
    blob = json.dumps(digest.build_payload(report), ensure_ascii=False)
    for forbidden in ("transaction_id", "tx_id", "balance", "баланс"):
        assert forbidden not in blob.lower()


def test_payload_sends_transfers_as_one_total_with_no_names():
    # Top merchants were approved for venues and services, not for people.
    rows = [{"date": datetime(2026, 7, 28), "kind": "Витрата",
             "category": "Перекази — людям / P2P", "merchant": "Іваненко Іван",
             "amount": 1500.0}]
    payload = digest.build_payload(analytics.build_report(rows, "week", date(2026, 8, 3)))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "Іваненко" not in blob
    assert payload["moved"] == 1500


def test_the_analysis_is_a_second_message(monkeypatch):
    monkeypatch.setattr(digest.openrouter_client, "analyze",
                        lambda *a, **kw: ("Спостереження.", "a:free"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    client = FakeClient()
    digest.send_digest(FakeSheets([spend_row(28, 7, 250.0)]), client, 42, "week",
                       date(2026, 8, 3))
    assert len(client.sent) == 2
    numbers, analysis = client.sent[0][1], client.sent[1][1]
    assert "Спостереження." in analysis
    assert "Аналіз" in analysis
    assert "ще не підключено" not in numbers


def test_a_silent_model_costs_nothing_but_the_analysis(monkeypatch):
    monkeypatch.setattr(digest.openrouter_client, "analyze", lambda *a, **kw: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    client = FakeClient()
    key = digest.send_digest(FakeSheets([spend_row(28, 7, 250.0)]), client, 42, "week",
                             date(2026, 8, 3))
    assert len(client.sent) == 1
    assert key == "2026-W31"
    assert "ще не підключено" in client.sent[0][1]


def test_an_exploding_model_cannot_stop_the_numbers(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("openrouter is on fire")

    monkeypatch.setattr(digest.openrouter_client, "analyze", boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    client = FakeClient()
    key = digest.send_digest(FakeSheets([spend_row(28, 7, 250.0)]), client, 42, "week",
                             date(2026, 8, 3))
    assert key == "2026-W31"
    assert len(client.sent) == 1


def test_the_model_that_answered_is_remembered(monkeypatch):
    monkeypatch.setattr(digest.openrouter_client, "analyze",
                        lambda *a, **kw: ("текст", "winner:free"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    state = dict(DEFAULTS)
    digest.send_digest(FakeSheets([spend_row(28, 7, 250.0)]), FakeClient(), 42, "week",
                       date(2026, 8, 3), state=state)
    assert state["model"] == "winner:free"


def test_the_remembered_model_is_offered_back(monkeypatch):
    seen = {}

    def spy(payload, api_key, prefer="", **kw):
        seen["prefer"] = prefer
        return ("текст", "winner:free")

    monkeypatch.setattr(digest.openrouter_client, "analyze", spy)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    digest.send_digest(FakeSheets([spend_row(28, 7, 250.0)]), FakeClient(), 42, "week",
                       date(2026, 8, 3), state=dict(DEFAULTS, model="last:free"))
    assert seen["prefer"] == "last:free"


def test_run_persists_the_model_alongside_the_period_key(monkeypatch):
    monkeypatch.setattr(digest.openrouter_client, "analyze",
                        lambda *a, **kw: ("текст", "winner:free"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setattr(digest, "TelegramClient", lambda token: FakeClient())
    sheets = FakeSheets([spend_row(28, 7, 250.0)])
    digest.run(sheets, "token", 42, today=date(2026, 8, 3))
    assert digest.parse_state(sheets.state_text)["model"] == "winner:free"


def outward_row(merchant, category="Послуги", amount=900.0):
    return {"date": datetime(2026, 7, 28), "kind": "Витрата",
            "category": category, "merchant": merchant, "amount": amount}


def payload_for(rows):
    return json.dumps(
        digest.build_payload(analytics.build_report(rows, "week", date(2026, 8, 3))),
        ensure_ascii=False)


def test_a_persons_full_name_never_leaves_the_machine():
    # Seen in practice: "Лисенко Олег Петрович" sat in Платежі за реквізитами, a
    # spend category, so it reached top_merchants untouched.
    blob = payload_for([outward_row("Лисенко Олег Петрович",
                                    category="Платежі за реквізитами")])
    assert "Лисенко" not in blob
    assert "Петрович" not in blob


def test_a_three_word_name_is_redacted_in_any_category():
    blob = payload_for([outward_row("Гончаренко Ольга Іванівна")])
    assert "Гончаренко" not in blob
    assert "особа" in blob


def test_contract_and_account_numbers_are_stripped():
    blob = payload_for([outward_row("Кредит готівкою 12345678901234",
                                    category="Кредит")])
    assert "12345678901234" not in blob
    assert "Кредит готівкою" in blob


def test_an_ordinary_merchant_survives_intact():
    blob = payload_for([outward_row("PORTMONE INSURANCE W, KYIV",
                                    category="Страхування")])
    assert "PORTMONE INSURANCE W, KYIV" in blob


def test_a_short_number_in_a_brand_name_is_kept():
    blob = payload_for([outward_row("WOG 24", category="Автомобіль")])
    assert "WOG 24" in blob


def test_the_balance_line_names_the_transaction_it_followed():
    # Never presented as "money you have": with several cards the column jumps
    # between them, so the reader needs to see which moment it belongs to.
    rows = [dict(spend_row(28, 7, 250.0, merchant="Сільпо"), balance=4955.72)]
    text = digest.format_report(analytics.build_report(rows, "week", date(2026, 8, 3)))
    assert "4 956" in text or "4 955" in text
    assert "28 лип" in text


def test_no_balance_means_no_line_rather_than_a_zero():
    rows = [dict(spend_row(28, 7, 250.0), balance=None)]
    text = digest.format_report(analytics.build_report(rows, "week", date(2026, 8, 3)))
    assert "Залишок" not in text


def test_the_balance_never_reaches_the_model():
    rows = [dict(spend_row(28, 7, 250.0), balance=4955.72)]
    payload = digest.build_payload(analytics.build_report(rows, "week", date(2026, 8, 3)))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "balance" not in blob
    assert "4955" not in blob.replace(" ", "")


def cycle_rows(cycle_spend, income=20000.0):
    """Salary on the 10th, plus one row inside the reported week (so the report
    is not empty) and one inside the running pay cycle (which drives pace)."""
    rows = [{"date": datetime(2026, m, 10), "kind": "Дохід", "category": "Зарплата",
             "merchant": "ТОВ", "amount": income, "balance": None}
            for m in (5, 6, 7, 8)]
    rows.append(dict(spend_row(5, 8, 100.0), balance=None))          # in the week
    rows.append(dict(spend_row(11, 8, cycle_spend), balance=None))   # in the cycle
    return rows


def test_the_pace_block_answers_in_hryvnia_not_in_two_percentages():
    # "Витрачено 90% доходу, минуло 94% циклу" asks the reader to compare two
    # percentages with different denominators and then work out what follows.
    # The owner read it and said it did not mean anything, so the verdict comes
    # first and the money answers the question.
    text = digest.format_report(
        analytics.build_report(cycle_rows(500.0), "week", date(2026, 8, 12)))
    assert "Має вистачити" in text
    assert "Лишилось" in text and "₴/день" in text
    assert "% доходу" not in text and "% циклу" not in text


def test_the_day_count_is_declined_correctly():
    from digest import _days
    assert [_days(n) for n in (1, 2, 3, 4, 5, 11, 12, 14, 21, 22, 25)] == \
        ["день", "дні", "дні", "дні", "днів", "днів", "днів", "днів",
         "день", "дні", "днів"]


def test_running_ahead_is_stated_not_just_marked():
    text = digest.format_report(
        analytics.build_report(cycle_rows(19000.0), "week", date(2026, 8, 12)))
    assert "швидше" in text


def test_the_balance_is_set_apart_from_the_prose():
    rows = [dict(spend_row(28, 7, 250.0), balance=4955.72)]
    text = digest.format_report(analytics.build_report(rows, "week", date(2026, 8, 3)))
    line = next(ln for ln in text.splitlines() if "Залишок" in ln)
    assert "<b>" in line
    assert "💳" in line


def test_a_surname_with_an_initial_is_redacted_too():
    # Tagging a transfer turns it into ordinary spend, so "Петренко А." would
    # reach top_merchants. Two tokens, so the three-word ПІБ rule missed it.
    for name in ("Петренко А.", "Мельник А.", "Шевчук К.", "Іваненко І.П."):
        blob = payload_for([outward_row(name, category="Ремонт")])
        assert name.split()[0] not in blob, name
        assert "особа" in blob

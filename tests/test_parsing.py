import logging

from categories import OTHER_INCOME, UNKNOWN
from gmail_service import GmailService
from tests.fixtures import (
    METRO, OWN_CARD_IN, OWN_CARD_OUT_CREDIT, STYPENDIA,
    NA_KARTKU, INCOMING_TRANSFER_FEE,
    REFUND_BOLT, REFUND_DIGITALOCEAN, ALIEXPRESS,
)


def test_metro_parses_amount_and_balance():
    d = GmailService.extract_transaction_data(METRO)
    assert d["amount"] == 8.0
    assert d["balance"] == 3418.20
    assert d["card"] == "5*11"


def parse(text):
    return GmailService.extract_transaction_data(text)


def test_own_card_incoming_leg_is_transfer():
    d = parse(OWN_CARD_IN)
    assert d["is_income"] == "Переказ"
    assert d["direction"] == "in"


def test_own_card_outgoing_leg_is_transfer_without_a_sign():
    d = parse(OWN_CARD_OUT_CREDIT)
    assert d["is_income"] == "Переказ"
    assert d["direction"] == "out"


def test_stypendia_without_sign_is_income():
    assert parse(STYPENDIA)["is_income"] == "Дохід"


def test_ordinary_expense_unchanged():
    assert parse(METRO)["is_income"] == "Витрата"


def test_debit_expense_fee_already_included():
    assert parse(NA_KARTKU)["amount"] == 2249.19


def test_credit_card_expense_adds_fee_on_top():
    # Кред. ліміт present -> the balance dropped by amount + fee
    assert parse(OWN_CARD_OUT_CREDIT)["amount"] == 10351.50


def test_incoming_deducts_fee():
    assert parse(INCOMING_TRANSFER_FEE)["amount"] == 10000.00
    assert parse(OWN_CARD_IN)["amount"] == 500.00


def test_refund_keeps_the_category_it_reverses():
    d = parse(REFUND_BOLT)
    assert d["is_income"] == "Дохід"
    assert d["category"] == "Таксі"
    assert d["merchant"] == "Bolt"


def test_refund_with_foreign_currency():
    d = parse(REFUND_DIGITALOCEAN)
    assert d["category"] == "Цифрові товари"
    assert d["amount"] == 44.25


def test_conversion_boilerplate_stripped_from_merchant():
    assert parse(ALIEXPRESS)["merchant"] == "AliExpress"


def test_merchant_html_entities_are_unescaped():
    snippet = "145.00UAH Ресторани, кафе, бари. McDonald&#39;s 4*22 16:03 Бал. 8150.00UAH"
    assert parse(snippet)["merchant"] == "McDonald's"


def test_rows_are_sorted_chronologically():
    rows = [
        ["2026-07-24", "09:21", "Витрата", "c", "m", 1, 2, "b"],
        ["2026-07-23", "18:57", "Витрата", "c", "m", 1, 2, "a"],
    ]
    assert [r[7] for r in GmailService.sort_rows(rows)] == ["a", "b"]


def test_uah_fee_is_not_applied_to_a_dollar_amount():
    # No (...₴) UAH equivalent here, so the amount stays in dollars - the
    # UAH-denominated commission must not be subtracted from it.
    snippet = ("+100$ Зарахування переказу: від IVAN 4*22 15:53 "
               "Комісія 50.00UAH Бал. 9876.40UAH")
    assert parse(snippet)["amount"] == 100.0


def test_explicit_sign_outranks_refund_wording():
    # A stated "-" means money went out even though the wording ("Повернення
    # кредиту") looks like a refund.
    snippet = "-1432.16₴ Повернення кредиту 5*11 10:00 Бал. 100.00₴"
    assert parse(snippet)["is_income"] == "Витрата"


def test_own_card_transfer_without_a_dot_categorizes_correctly():
    snippet = ("10050.00UAH Переказ на свою картку *4444 5*33 21:03 "
               "Бал. 41250.00UAH")
    d = parse(snippet)
    assert d["is_income"] == "Переказ"
    assert d["category"] == "Перекази — свої картки"


def test_refund_delimited_by_comma_keeps_reversed_category():
    snippet = "+51.1₴ Повернення, Таксі Bolt 5*11 08:33 Бал. 100.00₴"
    assert parse(snippet)["category"] == "Таксі"


def test_fee_without_kredit_limit_leaves_amount_unchanged_and_logs(caplog):
    # No "Кред. ліміт" clause -> fee assumed already included; the assumption
    # must be visible in the log, not silent.
    with caplog.at_level(logging.INFO):
        d = parse(NA_KARTKU)
    assert d["amount"] == 2249.19
    assert any("included" in r.message for r in caplog.records)


def test_unresolvable_incoming_is_labelled_other_income():
    snippet = "+4850.00₴ PIRAEUS_P2P, KYIV 5*11 14:16 Бал. 6930.25₴"
    d = parse(snippet)
    assert d["is_income"] == "Дохід"
    assert d["category"] == OTHER_INCOME


def test_unresolvable_outgoing_still_queues_for_telegram():
    snippet = "-2 249.19₴ На картку 5*11 13:39 Комісія 11.19₴ Бал. 7 340.15₴"
    d = parse(snippet)
    assert d["is_income"] == "Витрата"
    assert d["category"] == UNKNOWN


def test_resolvable_income_keeps_its_real_category():
    # Стипендія must NOT be swallowed by the catch-all.
    d = parse(STYPENDIA)
    assert d["category"] == "Стипендія"

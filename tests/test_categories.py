from categories import CANON, OTHER_INCOME, UNKNOWN, categorize


def test_known_raw_category_maps_to_canonical():
    assert categorize("Ресторани", "Aroma Kava") == "Кафе та ресторани"


def test_unknown_category_is_not_invented():
    # A raw category that is in neither CANON nor KEYWORDS must not become a
    # category by way of .capitalize(). Do not use Страхування/Краса/Освіта here:
    # those are real PrivatBank categories and this task adds them to CANON.
    assert categorize("Ювелірні вироби", "ZOLOTO SHOP") == UNKNOWN


def test_na_kartku_is_unresolved():
    assert categorize("На картку", "На картку") == UNKNOWN


def test_both_own_card_legs_get_the_same_category():
    assert categorize("Переказ на свою картку", "*4444") == "Перекази — свої картки"
    assert categorize("Переказ зі своєї картки *2222", "*2222") == "Перекази — свої картки"


def test_real_bank_categories_are_canonical():
    assert categorize("Страхування", "PZU") == "Страхування"
    assert categorize("Освіта", "КУРСИ") == "Освіта"
    assert categorize("Краса", "BEAUTY") == "Краса"


def test_other_income_is_a_canonical_label():
    assert OTHER_INCOME == "Інші надходження"
    assert OTHER_INCOME in CANON.values()


import categories

CHOICES = ["Продукти", "Таксі", "Кафе та ресторани", "Ремонт техніки"]


def test_an_exact_category_name_matches_itself():
    assert categories.match_text("Продукти", CHOICES) == "Продукти"


def test_matching_ignores_case_and_surrounding_space():
    assert categories.match_text("  продукти  ", CHOICES) == "Продукти"


def test_a_keyword_in_a_sentence_resolves_through_the_existing_vocabulary():
    # "таксі" is already a KEYWORDS entry for Таксі; free text should reuse it
    # rather than grow a second vocabulary that can drift from the first.
    assert categories.match_text("таксі додому", CHOICES) == "Таксі"


def test_a_merchant_name_in_the_text_resolves_too():
    assert categories.match_text("купив у сільпо", CHOICES) == "Продукти"


def test_text_that_matches_nothing_returns_empty():
    assert categories.match_text("за ремонт даху сусідові", CHOICES) == ""


def test_empty_text_returns_empty():
    assert categories.match_text("   ", CHOICES) == ""


def test_a_resolved_label_absent_from_the_offered_list_is_still_returned():
    # categorize() knows more labels than the sheet currently offers as
    # buttons. The caller decides what to do with one that is not on the list.
    assert categories.match_text("аптека", []) == "Аптеки"


def test_the_scrub_removes_card_tails_numbers_and_names():
    assert categories.scrub_merchant("Переказ на картку *3333") == "Переказ на картку"
    assert categories.scrub_merchant("Кредит готівкою 12345678901234") == "Кредит готівкою"
    assert categories.scrub_merchant("Лисенко Олег Петрович") == "особа"
    assert categories.scrub_merchant("Петренко А.") == "особа"
    assert categories.scrub_merchant("PORTMONE  W   KYIV") == "PORTMONE W KYIV"


def test_a_canon_word_inside_a_sentence_resolves():
    # CANON is keyed by the bank's exact category text, so categorize() only
    # ever looked it up whole. Free text is a sentence, and "продукти" sitting
    # in one is the commonest answer there is.
    assert categories.match_text("продукти на тиждень", CHOICES) == "Продукти"
    assert categories.match_text("оплатив послуги", CHOICES) == "Послуги"


def test_the_longest_canon_phrase_wins():
    # "супермаркети та продукти" and "продукти" both match; the specific one
    # must not lose to the generic by dictionary order.
    assert categories.match_text("супермаркети та продукти", CHOICES) == "Продукти"


def test_a_canon_word_must_stand_alone():
    # "авто" maps to Автомобіль; "автобус" is transport and must not match it.
    assert categories.match_text("квиток на автобус", CHOICES) != "Автомобіль"


def test_credit_words_resolve_locally_without_a_model():
    # The owner typed "Кредит"; there was no local match, so it went to a model
    # and came back "Переказ рідним" - a label that happened to be on the list
    # and was therefore written straight to the sheet.
    from categories import match_text
    choices = ["Продукти", "Переказ рідним"]
    assert match_text("Кредит", choices) == "Кредит"
    assert match_text("погашення кредиту", choices) == "Кредит"
    assert match_text("Кредит готівкою", choices) == "Кредит"


# --- The Type a row carries after a rule renames its category ---------------

from categories import EXPENSE, INTERNAL, OWN_CARDS, flow_after_override


def test_an_own_card_transfer_the_owner_calls_credit_becomes_spending():
    # *1111 is a credit card: 10 005 UAH went out across three transfers with
    # no matching return. analytics.classify drops a row by Type alone, so the
    # category alone would leave the money invisible in every report.
    assert flow_after_override(INTERNAL, "Кредит", "*1111") == EXPENSE


def test_a_genuine_own_card_move_stays_internal():
    assert flow_after_override(INTERNAL, OWN_CARDS, "*2222") == INTERNAL


def test_the_incoming_leg_is_never_turned_into_spending():
    # It reads the same in the Type column, but money arriving is not spending.
    assert flow_after_override(
        INTERNAL, "Кредит", "Переказ зі своєї картки *1111") == INTERNAL


def test_ordinary_rows_are_untouched():
    assert flow_after_override("Витрата", "Продукти", "Сільпо") == "Витрата"
    assert flow_after_override("Дохід", "Зарплата", "ТОВ") == "Дохід"

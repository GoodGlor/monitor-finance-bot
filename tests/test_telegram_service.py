import pytest
import ast
import inspect

from datetime import date, datetime

import telegram_service
import telegram_state
import main as main_module
from categories import CANON, UNKNOWN
from telegram_service import (
    ASK_COOLDOWN_HOURS, CATEGORY_CHOICES, MAX_ASKS_PER_ROW, OTHER_LABEL,
    SEND_PAUSE_SECONDS, SKIP_LABEL,
    ask_pending, build_keyboard, decode_callback, drain_answers, encode_callback,
    format_question, is_learnable, run_tagging, sanitize_choices,
)


def test_choices_cover_canon_and_are_sorted():
    assert set(CATEGORY_CHOICES) == set(CANON.values())
    assert CATEGORY_CHOICES == sorted(CATEGORY_CHOICES)
    assert len(CATEGORY_CHOICES) == len(set(CATEGORY_CHOICES))  # no duplicates


def test_callback_round_trip():
    assert decode_callback(encode_callback("19f6fa8bda3e7972", "Продукти")) == \
        ("19f6fa8bda3e7972", "Продукти")


def test_callback_survives_a_display_reorder_because_it_carries_the_label():
    # The whole point of CRITICAL 1: adding/removing/renaming a category must
    # never change what a previously-sent button writes, because the label -
    # not a positional index - IS the payload.
    payload = encode_callback("tx1", "Транспорт")
    assert decode_callback(payload) == ("tx1", "Транспорт")


def test_callback_fits_telegram_64_byte_limit():
    # Computed from the longest ACTUAL label, not a hardcoded number, so the
    # test keeps meaning if labels change.
    longest_label = max(CATEGORY_CHOICES + [OTHER_LABEL], key=lambda c: len(c.encode()))
    payload = encode_callback("19f6fa8bda3e7972", longest_label)
    assert len(payload.encode()) <= 64


def test_malformed_callback_returns_none():
    assert decode_callback("garbage") is None
    assert decode_callback("") is None
    assert decode_callback("tx1:") is None
    assert decode_callback(":Продукти") is None


def test_decode_splits_on_the_first_colon_only():
    # partition, not rpartition: a payload with no colon in the label at all
    # (labels never contain one) must still round-trip a tx_id unaffected by
    # anything after the first colon.
    assert decode_callback("tx1:Категорія:з:двокрапками") == \
        ("tx1", "Категорія:з:двокрапками")


def test_keyboard_renders_an_out_of_order_choice_and_encodes_the_label_itself():
    # Put the LAST alphabetical choice first in the supplied list: build_keyboard
    # must not re-sort it, and must still encode the label correctly.
    choices = [CATEGORY_CHOICES[-1]] + CATEGORY_CHOICES[:-1]
    rows = build_keyboard("tx1", choices)
    first_button = rows[0][0]
    assert first_button["text"] == CATEGORY_CHOICES[-1]        # displayed first
    tx, label = decode_callback(first_button["callback_data"])
    assert label == CATEGORY_CHOICES[-1]                       # and encoded correctly


def test_keyboard_has_three_per_row_and_an_inshe_button():
    rows = build_keyboard("tx1")
    assert all(len(r) <= 3 for r in rows)
    assert rows[-1][0]["text"] == OTHER_LABEL
    assert decode_callback(rows[-1][0]["callback_data"]) == ("tx1", OTHER_LABEL)


def test_keyboard_uses_the_supplied_choices():
    rows = build_keyboard("tx1", ["Комуналка", "Квартплата"])
    labels = [b["text"] for r in rows for b in r]
    assert labels == ["Комуналка", "Квартплата", OTHER_LABEL, SKIP_LABEL]


def test_keyboard_renders_a_supplied_list_in_the_exact_given_order():
    # A deliberately non-alphabetical order (utilities first) must survive
    # unchanged - this is the owner's curated Категорії tab order.
    choices = ["Комуналка", "Автомобіль", "Продукти"]
    rows = build_keyboard("tx1", choices)
    labels = [b["text"] for r in rows for b in r]
    assert labels == choices + [OTHER_LABEL, SKIP_LABEL]


def test_sanitize_choices_drops_a_label_too_long_for_a_telegram_button():
    # Budget: 64 bytes total - a 16-char tx_id and ':' leave 47 bytes, and
    # each Cyrillic character is 2 UTF-8 bytes, so 24+ characters overflow.
    too_long = "Категорія" * 4          # 36 chars, 72 bytes - well past the budget
    assert sanitize_choices([too_long]) == []


def test_sanitize_choices_drops_reserved_labels():
    assert sanitize_choices([OTHER_LABEL]) == []
    assert sanitize_choices([UNKNOWN]) == []


def test_sanitize_choices_keeps_a_normal_label():
    assert sanitize_choices(["Комуналка"]) == ["Комуналка"]


def test_sanitize_choices_mixes_valid_and_invalid():
    too_long = "Категорія" * 4
    assert sanitize_choices(["Комуналка", too_long, OTHER_LABEL, UNKNOWN, "Таксі"]) == \
        ["Комуналка", "Таксі"]


def test_na_kartku_is_not_learnable():
    assert is_learnable("На картку") is False
    assert is_learnable("   ") is False
    assert is_learnable("Cyberion") is True


def test_short_merchant_is_not_learnable():
    assert is_learnable("ABC") is False        # 3 chars, below the 4-char floor


def test_merchant_with_no_letters_is_not_learnable():
    assert is_learnable("1234") is False
    assert is_learnable("----") is False


def test_merchant_with_four_or_more_digits_is_not_learnable():
    # categories.apply_override treats a 4+-digit key as a card-last-4
    # wildcard, so a merchant like this would silently recategorize every
    # future transfer to a card ending 2345.
    assert is_learnable("ФОП Іванов 2345") is False


def test_normal_merchant_with_few_digits_is_still_learnable():
    assert is_learnable("Ресторан Х2") is True


class FakeClient:
    def __init__(self, updates, send_results=None):
        self._updates = updates
        self.answered = []
        self.sent = []
        self.edited = []
        # An iterator of results to hand back from send_message, falling back
        # to a truthy default once exhausted.
        self._send_results = iter(send_results) if send_results is not None else None

    def get_updates(self, offset):
        return self._updates

    def get_webhook_info(self):
        # Nothing held back: the watchdog's quiet path, which is what every
        # test here that is not about the watchdog wants.
        return {"url": "", "pending_update_count": 0}

    def answer_callback(self, callback_id):
        self.answered.append(callback_id)

    def edit_message(self, chat_id, message_id, text, keyboard=None):
        self.edited.append((chat_id, message_id, text, keyboard))
        return {"message_id": message_id}

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        if self._send_results is not None:
            try:
                return next(self._send_results)
            except StopIteration:
                return {"message_id": 1}
        return {"message_id": 1}


class FakeSheets:
    def __init__(self, fail_for=()):
        self.categories = []
        self.flows = []
        self.rules = []
        self._fail_for = set(fail_for)

    def set_category(self, row, category):
        if row in self._fail_for:
            raise RuntimeError(f'Sheets 500 for row {row}')
        self.categories.append((row, category))

    def set_flow(self, row, kind):
        self.flows.append((row, kind))

    def append_rule(self, merchant, category):
        self.rules.append((merchant, category))

    def button_categories(self):
        # Empty by default so run_tagging tests exercise the CATEGORY_CHOICES
        # fallback path, same as a deployment with no Категорії tab.
        return []


def callback_update(update_id, tx_id, label, cb_id="cb", chat_id=500,
                    message_id=900):
    return {"update_id": update_id,
            "callback_query": {"id": cb_id,
                               "data": encode_callback(tx_id, label),
                               "message": {"message_id": message_id,
                                           "chat": {"id": chat_id}}}}


def empty_state(chat_id=None):
    return {"offset": 0, "chat_id": chat_id, "asked": {}, "declined": [],
            "replies": {}, "proposed": {}, "ask_count": {},
            "undelivered_alert_at": 0}


def test_answer_sets_category_and_learns_a_rule():
    client = FakeClient([callback_update(10, "tx1", "Інтернет")])
    sheets = FakeSheets()
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == [(5, "Інтернет")]
    assert sheets.rules == [("Cyberion", "Інтернет")]
    assert state["offset"] == 11          # update_id + 1
    assert client.answered == ["cb"]


def test_unlearnable_merchant_sets_row_but_saves_no_rule():
    client = FakeClient([callback_update(3, "tx2", "Комуналка")])
    sheets = FakeSheets()
    pending = {"tx2": {"row": 9, "merchant": "На картку", "tx_id": "tx2"}}
    drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == [(9, "Комуналка")]
    assert sheets.rules == []


def test_inshe_invites_a_typed_answer_and_does_NOT_silence_the_row():
    # The bug this encodes: "Інше" used to mean "never ask me again", with no
    # reply of any kind. The owner pressed it meaning "let me type it" and 34
    # of 35 pending rows became permanently unanswerable in silence.
    client = FakeClient([callback_update(4, "tx3", OTHER_LABEL)])
    sheets = FakeSheets()
    pending = {"tx3": {"row": 2, "merchant": "X", "tx_id": "tx3"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == []
    assert sheets.rules == []
    assert client.answered == ["cb"]      # still acknowledged
    assert state["declined"] == []        # the row stays in the queue
    # The instruction lands on the very message the owner must swipe to reply
    # to, and the buttons stay in case they change their mind.
    chat_id, message_id, text, keyboard = client.edited[-1]
    assert (chat_id, message_id) == (500, 900)
    assert "Reply" in text
    assert keyboard, "the keyboard must survive so a button is still an option"


def test_skip_button_is_what_silences_a_row():
    client = FakeClient([callback_update(4, "tx3", SKIP_LABEL)])
    sheets = FakeSheets()
    pending = {"tx3": {"row": 2, "merchant": "X", "tx_id": "tx3"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == []
    assert state["declined"] == ["tx3"]


def test_unknown_category_label_is_ignored_not_written():
    # A renamed or removed category must fail safe instead of writing junk.
    client = FakeClient([callback_update(5, "tx4", "Категорія Якої Більше Немає")])
    sheets = FakeSheets()
    pending = {"tx4": {"row": 3, "merchant": "X", "tx_id": "tx4"}}
    drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == []
    assert sheets.rules == []


def test_answer_with_a_label_outside_the_supplied_choices_is_ignored():
    # The owner can edit the Категорії tab at any time, so a label offered
    # when the question was sent can legitimately no longer be valid by the
    # time the answer arrives - the same list that built the keyboard must
    # gate what's written.
    client = FakeClient([callback_update(9, "tx9", "Зарплата")])
    sheets = FakeSheets()
    pending = {"tx9": {"row": 4, "merchant": "X", "tx_id": "tx9"}}
    drain_answers(client, sheets, {"offset": 0, "chat_id": 500, "asked": {}, "declined": []},
                  pending, ["Комуналка"])
    assert sheets.categories == []          # Зарплата is no longer offered


def test_start_message_discovers_chat_id():
    client = FakeClient([{"update_id": 20,
                          "message": {"text": "/start", "chat": {"id": 777}}}])
    state = drain_answers(client, FakeSheets(), empty_state(), {})
    assert state["chat_id"] == 777
    assert state["offset"] == 21


def test_repeat_start_from_the_known_chat_is_not_a_warning(caplog):
    client = FakeClient([{"update_id": 40,
                          "message": {"text": "/start", "chat": {"id": 500}}}])
    with caplog.at_level("WARNING"):
        state = drain_answers(client, FakeSheets(),
                              {"offset": 0, "chat_id": 500, "asked": {}, "declined": []},
                              {}, CATEGORY_CHOICES)
    assert state["chat_id"] == 500
    assert caplog.records == []          # the owner re-sending /start is not suspicious


def test_start_from_a_different_chat_still_warns(caplog):
    client = FakeClient([{"update_id": 41,
                          "message": {"text": "/start", "chat": {"id": 999}}}])
    with caplog.at_level("WARNING"):
        state = drain_answers(client, FakeSheets(),
                              {"offset": 0, "chat_id": 500, "asked": {}, "declined": []},
                              {}, CATEGORY_CHOICES)
    assert state["chat_id"] == 500       # unchanged
    assert caplog.records                # and it IS flagged


def test_second_start_from_a_different_chat_does_not_change_chat_id():
    client = FakeClient([{"update_id": 21,
                          "message": {"text": "/start", "chat": {"id": 999}}}])
    state = drain_answers(client, FakeSheets(), empty_state(chat_id=777), {})
    assert state["chat_id"] == 777


def test_callback_from_a_foreign_chat_applies_nothing_but_still_answers():
    client = FakeClient([callback_update(6, "tx1", "Продукти", chat_id=666)])
    sheets = FakeSheets()
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert sheets.categories == []
    assert sheets.rules == []
    assert client.answered == ["cb"]      # no hung spinner


def test_callback_with_no_known_chat_id_yet_is_dropped_but_still_answered():
    # A lost/corrupt state file must not let ANY chat's callback apply - the
    # row is simply left pending until /start re-establishes the chat id.
    client = FakeClient([callback_update(9, "tx1", "Продукти", chat_id=500)])
    sheets = FakeSheets()
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, sheets, empty_state(chat_id=None), pending)
    assert sheets.categories == []
    assert sheets.rules == []
    assert client.answered == ["cb"]      # no hung spinner
    assert state["chat_id"] is None


def test_answer_for_an_unknown_or_already_answered_row_is_ignored():
    client = FakeClient([callback_update(7, "ghost", "Продукти")])
    sheets = FakeSheets()
    state = drain_answers(client, sheets, empty_state(chat_id=500), {})
    assert sheets.categories == []
    assert state["offset"] == 8           # still consumed, never re-delivered


def test_offset_advances_past_every_update_even_when_malformed():
    client = FakeClient([{"update_id": 30}, {"update_id": 31, "callback_query": {"id": "c", "data": "junk"}}])
    state = drain_answers(client, FakeSheets(), empty_state(chat_id=500), {})
    assert state["offset"] == 32


def test_callback_with_null_message_and_chat_does_not_raise():
    # The callback path must be as null-defensive as the /start path: a
    # payload with "message": null (or "chat": null) must not raise.
    client = FakeClient([{"update_id": 40,
                          "callback_query": {"id": "cb", "data": "tx1:Продукти",
                                             "message": None}}])
    sheets = FakeSheets()
    state = drain_answers(client, sheets, empty_state(chat_id=500), {})
    assert state["offset"] == 41
    assert client.answered == ["cb"]


def test_sheets_error_mid_drain_does_not_lose_the_offset_or_abort_the_batch():
    client = FakeClient([callback_update(1, "bad", "Продукти"),
                         callback_update(2, "good", "Продукти")])
    sheets = FakeSheets(fail_for={5})
    pending = {"bad": {"row": 5, "merchant": "X", "tx_id": "bad"},
              "good": {"row": 6, "merchant": "Y", "tx_id": "good"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert state["offset"] == 3           # both updates consumed
    assert sheets.categories == [(6, "Продукти")]   # the good row still applied


def test_question_names_the_amount_date_and_merchant():
    text = format_question({"date": "2026-07-17", "time": "13:39",
                            "merchant": "На картку", "amount": 2249.19, "tx_id": "t"})
    assert "2249.19" in text
    assert "2026-07-17" in text
    assert "На картку" in text


def test_ask_sends_a_summary_then_one_message_per_row_and_pauses(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: sleeps.append(s))
    client = FakeClient([])
    pending = [
        {"row": 2, "date": "2026-07-17", "time": "13:39", "merchant": "На картку",
         "amount": 2249.19, "tx_id": "t1"},
        {"row": 3, "date": "2026-07-18", "time": "10:00", "merchant": "На картку",
         "amount": 105, "tx_id": "t2"},
    ]
    sent = ask_pending(client, 500, pending, empty_state())
    assert sent == 2
    assert len(client.sent) == 3                 # 1 summary + 2 questions
    assert client.sent[0][2] is None             # summary carries no keyboard
    assert client.sent[1][2] is not None         # questions do
    assert "2" in client.sent[0][1]              # summary states the count
    assert sleeps == [SEND_PAUSE_SECONDS, SEND_PAUSE_SECONDS]


def test_ask_stops_at_the_first_failed_send_and_returns_the_true_count(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    # summary ok, first question ok, second question fails -> only 1 counted
    client = FakeClient([], send_results=[{"ok": True}, {"ok": True}, None])
    pending = [
        {"row": 2, "date": "d", "time": "t", "merchant": "m1", "amount": 1, "tx_id": "t1"},
        {"row": 3, "date": "d", "time": "t", "merchant": "m2", "amount": 1, "tx_id": "t2"},
        {"row": 4, "date": "d", "time": "t", "merchant": "m3", "amount": 1, "tx_id": "t3"},
    ]
    sent = ask_pending(client, 500, pending, empty_state())
    assert sent == 1
    assert len(client.sent) == 3                 # summary + 2 attempted questions (not the 3rd)


def test_ask_does_nothing_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    client = FakeClient([])
    assert ask_pending(client, 500, [], empty_state()) == 0
    assert client.sent == []


def test_ask_does_nothing_without_a_chat_id(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    client = FakeClient([])
    pending = [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                "amount": 1, "tx_id": "x"}]
    assert ask_pending(client, None, pending, empty_state()) == 0
    assert client.sent == []


def test_declined_row_is_never_re_asked(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    client = FakeClient([])
    pending = [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                "amount": 1, "tx_id": "declined-tx"}]
    state = empty_state()
    state["declined"] = ["declined-tx"]
    assert ask_pending(client, 500, pending, state) == 0
    assert client.sent == []


def test_row_asked_one_hour_ago_is_skipped(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    now = 1_000_000.0
    monkeypatch.setattr(telegram_service.time, 'time', lambda: now)
    client = FakeClient([])
    pending = [{"row": 2, "date": "d", "time": "t", "merchant": "m", "amount": 1, "tx_id": "tx"}]
    state = empty_state()
    state["asked"] = {"tx": now - 3600}            # 1 hour ago
    assert ask_pending(client, 500, pending, state) == 0
    assert client.sent == []


def test_row_asked_thirteen_hours_ago_is_asked_again(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    now = 1_000_000.0
    monkeypatch.setattr(telegram_service.time, 'time', lambda: now)
    client = FakeClient([])
    pending = [{"row": 2, "date": "d", "time": "t", "merchant": "m", "amount": 1, "tx_id": "tx"}]
    state = empty_state()
    state["asked"] = {"tx": now - 13 * 3600}       # 13 hours ago, past ASK_COOLDOWN_HOURS
    assert ASK_COOLDOWN_HOURS == 12
    sent = ask_pending(client, 500, pending, state)
    assert sent == 1
    assert state["asked"]["tx"] == now             # timestamp refreshed


def test_run_tagging_is_a_no_op_without_a_token(monkeypatch):
    called = []
    monkeypatch.setattr(telegram_service, 'TelegramClient',
                        lambda *a, **k: called.append(1))
    run_tagging(FakeSheets(), "", "/tmp/unused-state.json")
    assert called == []


def test_run_tagging_swallows_errors(monkeypatch, tmp_path):
    class Exploding:
        def pending_rows(self):
            raise RuntimeError("sheets down")
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: FakeClient([]))
    run_tagging(Exploding(), "TOKEN", str(tmp_path / "s.json"))  # must not raise


def test_run_tagging_prunes_asked_and_declined_for_resolved_rows(monkeypatch, tmp_path):
    from telegram_state import load_state, save_state

    state_path = str(tmp_path / "s.json")
    save_state(state_path, {"offset": 0, "chat_id": 500,
                            "asked": {"gone": 1.0, "stays": 1.0},
                            "declined": ["gone-too"],
                            "replies": {}, "proposed": {}, "ask_count": {},
                            "undelivered_alert_at": 0})

    class Sheets:
        def pending_rows(self):
            return [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                     "amount": 1, "tx_id": "stays"}]

        def category_frequencies(self):
            return {}

        def button_categories(self):
            return []

    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: FakeClient([]))
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(Sheets(), "TOKEN", state_path)

    saved = load_state(state_path)
    assert "gone" not in saved["asked"]
    assert "gone-too" not in saved["declined"]


def test_run_tagging_offers_categories_read_from_the_sheet(monkeypatch, tmp_path):
    # Every other run_tagging test's fake returns [] from button_categories,
    # exercising only the CATEGORY_CHOICES fallback. This proves the sheet's
    # own list actually reaches the keyboard, not just a hardcoded default.
    from telegram_state import save_state

    state_path = str(tmp_path / "s.json")
    save_state(state_path, {"offset": 0, "chat_id": 500, "asked": {},
                            "declined": [], "replies": {}, "proposed": {}, "ask_count": {},
                            "undelivered_alert_at": 0})

    class Sheets:
        def pending_rows(self):
            return [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                     "amount": 1, "tx_id": "tx1"}]

        def category_frequencies(self):
            return {}

        def button_categories(self):
            return ["Квартплата"]

    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(Sheets(), "TOKEN", state_path)

    question = client.sent[1]                       # sent[0] is the summary
    labels = [b["text"] for row in question[2] for b in row]
    assert "Квартплата" in labels
    # "Квартплата" isn't in CATEGORY_CHOICES, so if it's present the sheet's
    # list was really used; a CATEGORY_CHOICES-only label must not leak in.
    assert not set(CATEGORY_CHOICES) & set(labels)


def test_run_tagging_preserves_the_sheets_order_and_skips_the_frequency_lookup(
        monkeypatch, tmp_path):
    # The owner curated this order deliberately (utilities first); frequency
    # would rank "Автомобіль" first here, so if that happened the owner's
    # order was scrambled.
    from telegram_state import save_state

    state_path = str(tmp_path / "s.json")
    save_state(state_path, {"offset": 0, "chat_id": 500, "asked": {},
                            "declined": [], "replies": {}, "proposed": {}, "ask_count": {},
                            "undelivered_alert_at": 0})

    class Sheets:
        def __init__(self):
            self.frequency_calls = 0

        def pending_rows(self):
            return [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                     "amount": 1, "tx_id": "tx1"}]

        def category_frequencies(self):
            self.frequency_calls += 1
            return {"Автомобіль": 99, "Комуналка": 1}

        def button_categories(self):
            return ["Комуналка", "Автомобіль"]

    sheets = Sheets()
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(sheets, "TOKEN", state_path)

    question = client.sent[1]                       # sent[0] is the summary
    labels = [b["text"] for row in question[2] for b in row]
    assert labels[:2] == ["Комуналка", "Автомобіль"]  # sheet order preserved
    assert sheets.frequency_calls == 0               # never consulted


def test_run_tagging_fallback_list_is_still_frequency_sorted(monkeypatch, tmp_path):
    # No curated sheet list: the built-in fallback pool has no human order, so
    # frequency sorting must still apply there.
    from telegram_state import save_state

    state_path = str(tmp_path / "s.json")
    save_state(state_path, {"offset": 0, "chat_id": 500, "asked": {},
                            "declined": [], "replies": {}, "proposed": {}, "ask_count": {},
                            "undelivered_alert_at": 0})

    boosted = CATEGORY_CHOICES[-1]   # an otherwise-late label made most-used

    class Sheets:
        def pending_rows(self):
            return [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                     "amount": 1, "tx_id": "tx1"}]

        def category_frequencies(self):
            return {boosted: 999}

        def button_categories(self):
            return []

    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(Sheets(), "TOKEN", state_path)

    question = client.sent[1]
    labels = [b["text"] for row in question[2] for b in row]
    assert labels[0] == boosted


def test_run_tagging_falls_back_to_category_choices_when_the_tab_is_all_invalid(
        monkeypatch, tmp_path):
    from telegram_state import save_state

    state_path = str(tmp_path / "s.json")
    save_state(state_path, {"offset": 0, "chat_id": 500, "asked": {},
                            "declined": [], "replies": {}, "proposed": {}, "ask_count": {},
                            "undelivered_alert_at": 0})

    too_long = "Категорія" * 4   # well past the 64-byte callback_data budget

    class Sheets:
        def pending_rows(self):
            return [{"row": 2, "date": "d", "time": "t", "merchant": "m",
                     "amount": 1, "tx_id": "tx1"}]

        def category_frequencies(self):
            return {}

        def button_categories(self):
            return [OTHER_LABEL, UNKNOWN, too_long]

    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(Sheets(), "TOKEN", state_path)

    question = client.sent[1]
    labels = {b["text"] for row in question[2] for b in row}
    assert labels & set(CATEGORY_CHOICES)     # fell back to the constant pool
    assert too_long not in labels


def test_run_tagging_is_reachable_on_runs_with_no_new_mail():
    """The pipeline must not return early before the tagging loop.

    Most cron runs find no new mail; if the early return survived, answers
    would only ever be drained on runs that happened to receive an email.

    Anchored on whichever function actually calls run_tagging, so moving the
    pipeline body (as the failure-alert wrapper did) can't silently retire the
    guard by leaving it inspecting an empty `main`.
    """
    tree = ast.parse(inspect.getsource(main_module))
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "run_tagging"
                 for c in ast.walk(n))),
        None)
    assert func is not None, "no function in main.py calls run_tagging"
    body = func.body
    call_index = next(
        i for i, node in enumerate(body)
        if any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "run_tagging"
               for c in ast.walk(node)))
    earlier = body[:call_index]
    assert not any(isinstance(n, ast.Return) for stmt in earlier for n in ast.walk(stmt)), \
        "an early return before run_tagging would skip tagging on mail-free runs"


def test_run_tagging_defaults_to_sheet_backed_state(monkeypatch):
    # The whole point of the sheet backend: on a host with an ephemeral
    # filesystem nothing may be persisted to disk, or `asked` is lost every
    # run and every pending row is re-asked on every tick.
    import json

    class Sheets:
        def __init__(self):
            self.cell = None

        def pending_rows(self):
            return []

        def category_frequencies(self):
            return {}

        def button_categories(self):
            return []

        def read_state(self):
            return self.cell

        def write_state(self, text):
            self.cell = text

    sheets = Sheets()
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: FakeClient([]))
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    # No store argument at all - exactly how main.py calls it.
    run_tagging(sheets, "TOKEN")

    assert sheets.cell is not None, "state was never written to the sheet"
    assert set(json.loads(sheets.cell)) == {"offset", "chat_id", "asked",
                                            "declined", "replies", "proposed",
                                            "ask_count", "undelivered_alert_at"}


def test_run_tagging_never_touches_disk_when_sheet_backed(monkeypatch):
    # Guards the ephemeral-filesystem contract directly: if a future change
    # reintroduces a file write, this fails rather than silently degrading in
    # production, where the symptom is a bot that re-asks everything forever.
    import builtins

    class Sheets:
        def __init__(self):
            self.cell = None

        def pending_rows(self):
            return []

        def category_frequencies(self):
            return {}

        def button_categories(self):
            return []

        def read_state(self):
            return self.cell

        def write_state(self, text):
            self.cell = text

    opened_for_write = []
    real_open = builtins.open

    def spy(path, mode='r', *a, **k):
        if any(m in mode for m in ('w', 'a', 'x', '+')):
            opened_for_write.append(str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, 'open', spy)
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: FakeClient([]))
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    run_tagging(Sheets(), "TOKEN")

    assert opened_for_write == [], f'wrote to disk: {opened_for_write}'


# --- tap confirmation --------------------------------------------------------
#
# The run that applies a tap is never the run that asked: by then Telegram's
# callback id has expired and answerCallbackQuery fails, so the button itself
# can give no feedback. These cover the message that replaces it.


def test_format_confirmation_lists_what_was_applied():
    text = telegram_service.format_confirmation([
        {"tx_id": "a", "label": "Продукти", "merchant": "ATB"},
        {"tx_id": "b", "label": "Інтернет", "merchant": "Cyberion"},
    ])
    assert "✅ Категоризовано: 2" in text
    assert "• ATB → Продукти" in text
    assert "• Cyberion → Інтернет" in text


def test_format_confirmation_truncates_a_long_list():
    applied = [{"tx_id": str(i), "label": "Продукти", "merchant": f'M{i}'}
               for i in range(14)]
    text = telegram_service.format_confirmation(applied)
    assert "✅ Категоризовано: 14" in text
    assert text.count("•") == 10
    assert "…та ще 4" in text


def test_format_confirmation_handles_a_blank_merchant():
    text = telegram_service.format_confirmation(
        [{"tx_id": "a", "label": "Продукти", "merchant": "  "}])
    assert "• — → Продукти" in text


def test_drain_answers_reports_what_it_applied():
    client = FakeClient([callback_update(10, "tx1", "Інтернет")])
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, FakeSheets(), empty_state(chat_id=500), pending)
    assert state["applied"] == [{"tx_id": "tx1", "label": "Інтернет",
                                 "merchant": "Cyberion"}]


def test_a_failed_write_is_not_reported_as_applied():
    # The confirmation must never claim a category that never reached the sheet.
    client = FakeClient([callback_update(10, "tx1", "Інтернет")])
    sheets = FakeSheets(fail_for=(5,))
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)
    assert state["applied"] == []


def test_declining_is_not_reported_as_applied():
    client = FakeClient([callback_update(10, "tx1", telegram_service.SKIP_LABEL)])
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, FakeSheets(), empty_state(chat_id=500), pending)
    assert state["applied"] == []
    assert state["declined"] == ["tx1"]


def _confirming_sheets(pending_rows):
    class Sheets(FakeSheets):
        def __init__(self):
            super().__init__()
            self.cell = None
            self._pending = pending_rows

        def pending_rows(self):
            return list(self._pending)

        def category_frequencies(self):
            return {}

        def read_state(self):
            return self.cell

        def write_state(self, text):
            self.cell = text

    return Sheets()


def test_run_tagging_confirms_applied_answers(monkeypatch):
    import json

    row = {"row": 5, "date": "d", "time": "t", "merchant": "Cyberion",
           "amount": 1, "tx_id": "tx1"}
    sheets = _confirming_sheets([row])
    client = FakeClient([callback_update(10, "tx1", "Інтернет")])

    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    monkeypatch.setattr(telegram_service, 'CATEGORY_CHOICES', ["Інтернет"])
    sheets.cell = json.dumps({"offset": 0, "chat_id": 500, "asked": {}, "declined": []})

    run_tagging(sheets, "TOKEN")

    confirmations = [t for _, t, _ in client.sent if "Категоризовано" in t]
    assert len(confirmations) == 1
    assert "Cyberion → Інтернет" in confirmations[0]
    # The transient key must never reach storage.
    assert "applied" not in json.loads(sheets.cell)


def test_run_tagging_sends_no_confirmation_when_nothing_was_applied(monkeypatch):
    import json

    sheets = _confirming_sheets([])
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    sheets.cell = json.dumps({"offset": 0, "chat_id": 500, "asked": {}, "declined": []})

    run_tagging(sheets, "TOKEN")

    assert [t for _, t, _ in client.sent if "Категоризовано" in t] == []


# --- failure alerts ----------------------------------------------------------


def _at(minute):
    from datetime import datetime, timezone
    return datetime(2026, 7, 28, 12, minute, tzinfo=timezone.utc)


def test_should_alert_only_early_in_the_hour():
    assert telegram_service.should_alert(_at(0))
    assert telegram_service.should_alert(_at(9))
    assert not telegram_service.should_alert(_at(10))
    assert not telegram_service.should_alert(_at(59))


def test_notify_failure_sends_once_inside_the_window(monkeypatch):
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    assert telegram_service.notify_failure("TOKEN", 500, RuntimeError("boom"), _at(3))
    chat_id, text, _ = client.sent[0]
    assert chat_id == 500
    assert "❌ Monitor Finance" in text and "RuntimeError: boom" in text


def test_notify_failure_is_throttled_outside_the_window(monkeypatch):
    # A broken token would otherwise fire on all 144 runs a day.
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    assert not telegram_service.notify_failure("TOKEN", 500, RuntimeError("boom"), _at(37))
    assert client.sent == []


def test_notify_failure_without_a_chat_id_is_a_no_op(monkeypatch):
    called = []
    monkeypatch.setattr(telegram_service, 'TelegramClient',
                        lambda *a, **k: called.append(1))
    assert not telegram_service.notify_failure("TOKEN", None, RuntimeError("boom"), _at(3))
    assert called == []


def test_notify_failure_truncates_a_huge_error(monkeypatch):
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    telegram_service.notify_failure("TOKEN", 500, RuntimeError("x" * 5000), _at(3))
    assert len(client.sent[0][1]) < 500


def test_notify_failure_never_raises(monkeypatch):
    # It runs inside an exception handler: failing to report a failure must
    # never replace the original error.
    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(telegram_service, 'TelegramClient', Exploding)
    assert not telegram_service.notify_failure("TOKEN", 500, ValueError("orig"), _at(3))


def test_main_reports_a_failure_and_re_raises(monkeypatch):
    # The whole point: an unattended run that dies must say so somewhere the
    # owner actually looks.
    reported = []
    monkeypatch.setattr(main_module, 'notify_failure',
                        lambda token, chat, err: reported.append((token, chat, str(err))))

    def explode(*a, **k):
        raise RuntimeError("auth is broken")

    monkeypatch.setattr(main_module, '_run', explode)
    # Pinned: main() exits early outside the active window, so without this the
    # test silently passes or fails depending on the clock when it is run.
    monkeypatch.setattr(main_module, 'within_active_hours', lambda now=None: True)
    monkeypatch.setenv('TELEGRAM_API', 'TOKEN')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '500')

    try:
        main_module.main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("main() must re-raise so the scheduler records a failure")

    assert reported == [("TOKEN", "500", "auth is broken")]


# --- staleness ---------------------------------------------------------------


def _dt(day, hour=12, minute=0):
    from datetime import datetime
    return datetime(2026, 7, day, hour, minute)


def test_is_stale_only_past_the_threshold():
    now = _dt(28)
    assert not telegram_service.is_stale(_dt(27), now)          # 1 day
    assert not telegram_service.is_stale(_dt(26, 13), now)      # 47 hours
    assert telegram_service.is_stale(_dt(26, 11), now)          # 49 hours


def test_unknown_latest_is_not_stale():
    # A failed read must not be reported as a broken feed.
    assert not telegram_service.is_stale(None, _dt(28)) 


def test_should_alert_daily_fires_once_a_day():
    hour = telegram_service.STALE_ALERT_HOUR
    assert telegram_service.should_alert_daily(_dt(28, hour, 0))
    assert telegram_service.should_alert_daily(_dt(28, hour, 9))
    assert not telegram_service.should_alert_daily(_dt(28, hour, 10))
    assert not telegram_service.should_alert_daily(_dt(28, hour + 1, 0))


def test_stale_alert_hour_falls_inside_the_active_window():
    # Otherwise the run that would send the reminder never happens.
    assert main_module.ACTIVE_START_HOUR <= telegram_service.STALE_ALERT_HOUR \
        < main_module.ACTIVE_END_HOUR


def test_notify_stale_reports_the_age_and_the_last_transaction(monkeypatch):
    client = FakeClient([])
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda *a, **k: client)
    assert telegram_service.notify_stale("TOKEN", 500, _dt(26, 10), _dt(28, 12))
    text = client.sent[0][1]
    assert "50 год" in text                      # 2026-07-26 10:00 -> 07-28 12:00
    assert "2026-07-26 10:00" in text


def test_notify_stale_never_raises(monkeypatch):
    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(telegram_service, 'TelegramClient', Exploding)
    assert not telegram_service.notify_stale("TOKEN", 500, _dt(26), _dt(28))


# --- active hours ------------------------------------------------------------


def test_within_active_hours_covers_the_configured_window():
    # Derived from the constants, not from hardcoded hours: the window has
    # moved once already and a test that pins 05:00 only records history.
    start, end = main_module.ACTIVE_START_HOUR, main_module.ACTIVE_END_HOUR
    assert not main_module.within_active_hours(_dt(28, start - 1, 59))
    assert main_module.within_active_hours(_dt(28, start, 0))
    assert main_module.within_active_hours(_dt(28, end - 1, 50))
    assert not main_module.within_active_hours(_dt(28, end, 0))    # end is exclusive


def test_main_does_no_work_outside_active_hours(monkeypatch):
    called = []
    monkeypatch.setattr(main_module, '_run', lambda *a: called.append(1))
    monkeypatch.setattr(main_module, 'within_active_hours', lambda: False)
    main_module.main()
    assert called == [], "the pipeline must not run during quiet hours"


def test_main_runs_inside_active_hours(monkeypatch):
    called = []
    monkeypatch.setattr(main_module, '_run', lambda *a: called.append(1))
    monkeypatch.setattr(main_module, 'within_active_hours', lambda: True)
    main_module.main()
    assert called == [1]


import digest as digest_module


def command_update(update_id, text, chat_id=500):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text}}


def test_week_command_sends_a_report_without_touching_digest_state(monkeypatch):
    calls = []
    monkeypatch.setattr(digest_module, "send_digest",
                        lambda *args, **kw: calls.append(args[3]) or "2026-W31")
    client = FakeClient([command_update(1, "/week")])
    state = empty_state(chat_id=500)

    result = telegram_service.drain_answers(client, FakeSheets(), state, {})

    assert calls == ["week"]
    # The scheduled report's ledger is untouched by a manual request.
    assert "last_weekly" not in result and "last_weekly" not in state
    assert result["offset"] == 2


def test_month_command_is_handled_too(monkeypatch):
    calls = []
    monkeypatch.setattr(digest_module, "send_digest",
                        lambda *args, **kw: calls.append(args[3]) or "2026-07")
    client = FakeClient([command_update(1, "/month")])
    telegram_service.drain_answers(client, FakeSheets(), empty_state(chat_id=500), {})
    assert calls == ["month"]


def test_a_command_from_a_stranger_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(digest_module, "send_digest",
                        lambda *args, **kw: calls.append(args[3]))
    client = FakeClient([command_update(1, "/week", chat_id=999)])
    telegram_service.drain_answers(client, FakeSheets(), empty_state(chat_id=500), {})
    assert calls == []


def test_a_command_before_the_chat_id_is_known_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(digest_module, "send_digest",
                        lambda *args, **kw: calls.append(args[3]))
    client = FakeClient([command_update(1, "/week")])
    telegram_service.drain_answers(client, FakeSheets(), empty_state(), {})
    assert calls == []


def test_a_failing_report_command_cannot_wedge_the_tagging_loop(monkeypatch):
    # The offset advances past the command even when the digest raises.
    # Without this, the same /week update is redelivered every run, raises
    # again, and the offset is never persisted - the loop stops applying
    # answers and asking questions, permanently.
    def boom(*args, **kw):
        raise RuntimeError("Sheets 500")

    monkeypatch.setattr(digest_module, "send_digest", boom)
    client = FakeClient([command_update(7, "/week")])
    state = empty_state(chat_id=500)

    result = telegram_service.drain_answers(client, FakeSheets(), state, {})

    assert result["offset"] == 8


def test_a_transfer_is_asked_about_differently_from_an_unknown_row():
    unknown = format_question({"date": "07.08.2026", "time": "18:15",
                               "amount": 250, "merchant": "На картку",
                               "kind": "unknown"})
    transfer = format_question({"date": "07.08.2026", "time": "18:15",
                                "amount": 7500, "merchant": "Петренко А.",
                                "kind": "transfer"})
    assert "Оберіть категорію" in unknown
    # A transfer already has a category; what is missing is its purpose.
    assert "за що" in transfer.lower()
    assert "Петренко А." in transfer


def test_a_row_with_no_kind_still_gets_the_default_question():
    assert "Оберіть категорію" in format_question({"merchant": "На картку"})


def many_pending(n):
    return [{"tx_id": f"t{i}", "row": i + 2, "kind": "unknown",
             "date": "07.08.2026", "time": "10:00",
             "merchant": f"M{i}", "amount": float(i)} for i in range(n)]


@pytest.fixture
def no_send_pause(monkeypatch):
    """ask_pending paces its sends; the pacing is not what these tests check."""
    monkeypatch.setattr(telegram_service.time, "sleep", lambda seconds: None)


def test_a_run_asks_at_most_a_handful_of_questions(no_send_pause):
    # 37 rows became askable at once; sending all of them buries the few the
    # owner actually wants to answer.
    client = FakeClient([])
    state = empty_state(chat_id=500)
    sent = telegram_service.ask_pending(client, 500, many_pending(37), state)
    assert sent == telegram_service.MAX_QUESTIONS_PER_RUN
    # one summary plus the questions
    assert len(client.sent) == telegram_service.MAX_QUESTIONS_PER_RUN + 1


def test_the_biggest_amounts_are_asked_about_first(no_send_pause):
    client = FakeClient([])
    state = empty_state(chat_id=500)
    telegram_service.ask_pending(client, 500, many_pending(20), state)
    asked = [t for _, t, kb in client.sent[1:]]
    assert "M19" in asked[0]


def test_the_summary_counts_what_is_actually_being_asked(no_send_pause):
    client = FakeClient([])
    state = empty_state(chat_id=500)
    telegram_service.ask_pending(client, 500, many_pending(37), state)
    summary = client.sent[0][1]
    assert str(telegram_service.MAX_QUESTIONS_PER_RUN) in summary
    assert "37" in summary          # the backlog is still visible


def test_each_movement_kind_gets_its_own_question():
    base = {"date": "07.08.2026", "time": "18:15", "amount": 7500}
    cash = format_question(dict(base, merchant="Банкомат", kind="cash"))
    incoming = format_question(dict(base, merchant="Коваль Ю.", kind="incoming"))
    assert "готівк" in cash.lower()
    assert "звідки" in incoming.lower()


class IdClient(FakeClient):
    """FakeClient whose sends come back with increasing message ids."""

    def send_message(self, chat_id, text, keyboard=None, parse_mode=None):
        super().send_message(chat_id, text, keyboard)
        return {"message_id": 100 + len(self.sent)}


ONE_PENDING = [{"tx_id": "tx1", "row": 2, "kind": "unknown", "date": "07.08.2026",
                "time": "10:00", "merchant": "На картку", "amount": 250.0}]


def test_asking_records_which_question_each_message_was(no_send_pause):
    client = IdClient([])
    state = empty_state(chat_id=500)
    state["replies"] = {}

    telegram_service.ask_pending(client, 500, list(ONE_PENDING), state)

    # the summary is message 101, the question 102
    assert state["replies"] == {"102": "tx1"}


def test_a_send_that_returns_nothing_records_no_reply_target(no_send_pause):
    client = FakeClient([], send_results=[{"message_id": 1}, None])
    state = empty_state(chat_id=500)
    state["replies"] = {}
    telegram_service.ask_pending(client, 500, list(ONE_PENDING), state)
    assert state["replies"] == {}


def reply_update(update_id, message_id, text, chat_id=500):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text,
                        "reply_to_message": {"message_id": message_id}}}


def tagging_state(chat_id=500, replies=None):
    state = empty_state(chat_id=chat_id)
    state["replies"] = dict(replies or {"102": "tx1"})
    state["proposed"] = {}
    return state


PENDING = {"tx1": {"row": 5, "merchant": "На картку", "tx_id": "tx1"}}


def test_a_locally_matched_reply_sets_the_category(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a local match must not call the model")

    monkeypatch.setattr(telegram_service.openrouter_client,
                        "propose_category", explode)
    client = FakeClient([reply_update(1, 102, "таксі додому")])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING,
                                   ["Таксі", "Продукти"])
    assert sheets.categories == [(5, "Таксі")]


def test_an_unmatched_reply_asks_the_model_and_proposes(monkeypatch):
    monkeypatch.setattr(telegram_service.openrouter_client, "propose_category",
                        lambda *a, **kw: "Ремонт даху")
    client = FakeClient([reply_update(1, 102, "платив за дах")])
    sheets = FakeSheets()
    state = tagging_state()
    telegram_service.drain_answers(client, sheets, state, PENDING, ["Таксі"])
    assert sheets.categories == []          # nothing written until confirmed
    assert state["proposed"] == {"tx1": "Ремонт даху"}
    assert "Ремонт даху" in client.sent[-1][1]


def test_a_reply_to_an_unknown_message_is_answered_not_guessed():
    client = FakeClient([reply_update(1, 999, "таксі")])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING, ["Таксі"])
    assert sheets.categories == []
    assert "конкретне питання" in client.sent[-1][1]


def test_text_that_is_not_a_reply_is_answered_not_guessed():
    client = FakeClient([{"update_id": 1,
                          "message": {"chat": {"id": 500}, "text": "таксі"}}])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING, ["Таксі"])
    assert sheets.categories == []
    assert "конкретне питання" in client.sent[-1][1]


def test_a_reply_about_a_row_already_answered_is_ignored():
    client = FakeClient([reply_update(1, 102, "таксі")])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), {}, ["Таксі"])
    assert sheets.categories == []


def test_a_reply_from_a_stranger_is_ignored():
    client = FakeClient([reply_update(1, 102, "таксі", chat_id=999)])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING, ["Таксі"])
    assert sheets.categories == []
    assert client.sent == []


def test_a_silent_model_leaves_the_row_pending_and_says_so(monkeypatch):
    monkeypatch.setattr(telegram_service.openrouter_client, "propose_category",
                        lambda *a, **kw: None)
    client = FakeClient([reply_update(1, 102, "щось незрозуміле")])
    sheets = FakeSheets()
    state = tagging_state()
    telegram_service.drain_answers(client, sheets, state, PENDING, ["Таксі"])
    assert sheets.categories == []
    assert state["proposed"] == {}
    assert "не розпізнав" in client.sent[-1][1].lower()


def test_a_command_is_not_treated_as_an_answer(monkeypatch):
    monkeypatch.setattr(digest_module, "send_digest", lambda *a, **kw: "2026-W31")
    client = FakeClient([reply_update(1, 102, "/week")])
    sheets = FakeSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING, ["Таксі"])
    assert sheets.categories == []


class ProposingSheets(FakeSheets):
    def __init__(self, fail_for=()):
        super().__init__(fail_for)
        self.added = []

    def append_button_category(self, label):
        self.added.append(label)
        return True


def test_confirming_a_proposal_adds_the_category_and_applies_it():
    client = FakeClient([callback_update(10, "tx1", "Ремонт даху")])
    sheets = ProposingSheets()
    state = tagging_state()
    state["proposed"] = {"tx1": "Ремонт даху"}
    telegram_service.drain_answers(client, sheets, state, PENDING, ["Таксі"])
    assert sheets.added == ["Ремонт даху"]
    assert sheets.categories == [(5, "Ремонт даху")]
    assert state["proposed"] == {}


def test_an_off_list_label_with_no_matching_proposal_is_still_refused():
    # The guard that makes a renamed or removed button fail safe.
    client = FakeClient([callback_update(10, "tx1", "Вигадана")])
    sheets = ProposingSheets()
    telegram_service.drain_answers(client, sheets, tagging_state(), PENDING, ["Таксі"])
    assert sheets.added == []
    assert sheets.categories == []


def test_a_proposal_made_for_another_row_does_not_authorise_this_one():
    client = FakeClient([callback_update(10, "tx1", "Ремонт даху")])
    sheets = ProposingSheets()
    state = tagging_state()
    state["proposed"] = {"tx2": "Ремонт даху"}
    telegram_service.drain_answers(client, sheets, state, PENDING, ["Таксі"])
    assert sheets.categories == []


def test_ledgers_are_pruned_for_rows_that_left_the_queue():
    state = {"offset": 0, "chat_id": 5, "asked": {"gone": 1.0, "here": 2.0},
             "declined": ["gone", "here"], "replies": {"1": "gone", "2": "here"},
             "proposed": {"gone": "X", "here": "Y"}}
    telegram_service.prune_state(state, {"here"})
    assert state["asked"] == {"here": 2.0}
    assert state["declined"] == ["here"]
    assert state["replies"] == {"2": "here"}
    assert state["proposed"] == {"here": "Y"}


def test_the_digest_runs_from_the_pipeline_and_cannot_break_it(monkeypatch):
    # Heroku Scheduler has no CLI for jobs, so a second scheduled job is a
    # manual dashboard step that never got done. The digest rides the existing
    # ten-minute run instead - last, and unable to raise.
    def boom(*args, **kwargs):
        raise RuntimeError("digest is on fire")

    monkeypatch.setattr(main_module.digest, "run", boom)
    main_module._maybe_digest(object(), "token", 5,
                              datetime(2026, 8, 10, 10, 0))   # must not raise


def test_the_digest_waits_until_a_civilised_hour(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module.digest, "run",
                        lambda *a, **kw: calls.append(kw.get("today")))
    main_module._maybe_digest(object(), "token", 5, datetime(2026, 8, 10, 5, 30))
    assert calls == []          # 05:30 would be a notification that wakes you
    main_module._maybe_digest(object(), "token", 5, datetime(2026, 8, 10, 9, 0))
    assert calls == [date(2026, 8, 10)]


def test_the_digest_is_called_after_the_tagging_loop():
    # Same reasoning as run_tagging's own position: rows are already committed
    # by the time either runs, so neither can cost the pipeline anything.
    source = inspect.getsource(main_module._run)
    assert source.index("run_tagging(") < source.index("_maybe_digest(")


# --- The re-ask loop: one live question per row, and a bound on repeats ------
#
# The defect these encode: a pending row was re-asked every ASK_COOLDOWN_HOURS
# forever, and the previous question stayed in the chat with a working
# keyboard. Three identical live questions is what "I answer the same question
# three times" was, and answering any but the newest did nothing and said
# nothing - which read as "my answer was not recorded".

def test_re_asking_retires_the_previous_question_first(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    # Two results: ask_pending sends the summary first, then the question.
    client = FakeClient([], send_results=[{"message_id": 800}, {"message_id": 901}])
    state = empty_state(chat_id=500)
    state["replies"] = {"900": "tx"}          # a question already outstanding
    pending = [{"tx_id": "tx", "row": 2, "merchant": "Cyberion", "amount": 10}]

    assert ask_pending(client, 500, pending, state) == 1

    retired = [e for e in client.edited if e[1] == 900]
    assert retired, "the old question was left live alongside the new one"
    assert retired[0][3] in (None, [])       # its keyboard is gone
    # Unbound, or the next retirement edits it again with identical text and
    # Telegram rejects that as "message is not modified".
    assert state["replies"] == {"901": "tx"}


def test_a_row_goes_quiet_after_max_asks(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    client = FakeClient([])
    state = empty_state(chat_id=500)
    state["ask_count"] = {"tx": MAX_ASKS_PER_ROW}
    pending = [{"tx_id": "tx", "row": 2, "merchant": "X", "amount": 10}]

    assert ask_pending(client, 500, pending, state) == 0
    assert client.sent == []                 # not even the summary


def test_each_question_sent_increments_the_row_ask_count(monkeypatch):
    monkeypatch.setattr(telegram_service.time, 'sleep', lambda s: None)
    state = empty_state(chat_id=500)
    pending = [{"tx_id": "tx", "row": 2, "merchant": "X", "amount": 10}]

    ask_pending(FakeClient([]), 500, pending, state)
    assert state["ask_count"] == {"tx": 1}


# --- Feedback: the button must say something --------------------------------

def test_applying_an_answer_rewrites_the_question_and_drops_the_keyboard():
    # answerCallbackQuery cannot acknowledge anything here: the run that
    # applies a tap is never the run that sent the question, so Telegram has
    # already expired the callback id. Editing the message is the only
    # acknowledgement a cron-driven bot can give.
    client = FakeClient([callback_update(10, "tx1", "Інтернет")])
    state = empty_state(chat_id=500)
    state["replies"] = {"900": "tx1"}
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}

    drain_answers(client, FakeSheets(), state, pending)

    chat_id, message_id, text, keyboard = client.edited[-1]
    assert (chat_id, message_id) == (500, 900)
    assert "Cyberion" in text and "Інтернет" in text
    assert keyboard in (None, [])


def test_tapping_an_already_answered_question_says_so_instead_of_nothing():
    # Silence here is what made the owner tap again, and again.
    client = FakeClient([callback_update(11, "gone", "Інтернет")])
    drain_answers(client, FakeSheets(), empty_state(chat_id=500), {})
    assert client.edited, "a stale tap was dropped in silence"
    assert "категоризовано" in client.edited[-1][2]


def test_a_failed_rule_write_does_not_report_the_category_as_lost():
    # The category HAS landed; only the merchant shortcut for future rows was
    # not saved. Reporting that as "не вдалося записати" told the owner their
    # answer was gone when it was not.
    class RuleFails(FakeSheets):
        def append_rule(self, merchant, category):
            raise RuntimeError("Sheets 500")

    client = FakeClient([callback_update(12, "tx1", "Інтернет")])
    sheets = RuleFails()
    pending = {"tx1": {"row": 5, "merchant": "Cyberion", "tx_id": "tx1"}}
    state = drain_answers(client, sheets, empty_state(chat_id=500), pending)

    assert sheets.categories == [(5, "Інтернет")]
    assert [a["label"] for a in state["applied"]] == ["Інтернет"]
    assert not any("Не вдалося" in text for _, text, _ in client.sent)


def test_ask_count_is_pruned_with_the_other_ledgers():
    state = empty_state()
    state["ask_count"] = {"gone": 2, "stays": 1}
    telegram_service.prune_state(state, {"stays"})
    assert state["ask_count"] == {"stays": 1}


def test_skip_label_is_reserved_and_never_offered_as_a_category():
    assert sanitize_choices([SKIP_LABEL]) == []


# --- Keyboard packing: a truncated label is a wrong tap waiting to happen ----

LIVE_CATEGORIES = [
    'Комуналка', 'Квартплата', 'Переказ рідним', 'Переказ друзям', 'Медицина',
    'Краса', 'Освіта', 'Дім та ремонт', 'Ремонт техніки', 'Одяг та взуття',
    'Спорт', 'Подарунки', 'Благодійність', 'Тварини', 'Автомобіль',
    'Кафе та ресторани', 'Продукти', 'Інтернет', 'Квіти', 'Розваги',
]


def test_no_button_label_is_truncated_at_its_row_width():
    # Fixed 3-per-row rendered "Переказ рі…" and "Переказ др…" - two different
    # categories, indistinguishable on screen, one tap apart.
    from telegram_service import ROW_BUDGET
    for row in build_keyboard("tx1", LIVE_CATEGORIES):
        allowed = telegram_service.ROW_BUDGET // len(row)
        for button in row:
            assert len(button["text"]) <= allowed, \
                f'{button["text"]!r} is cut off in a row of {len(row)}'


def test_packing_never_reorders_the_owners_curated_list():
    rows = build_keyboard("tx1", LIVE_CATEGORIES)
    shown = [b["text"] for r in rows for b in r]
    assert shown[:len(LIVE_CATEGORIES)] == LIVE_CATEGORIES


def test_the_two_confusable_transfer_labels_are_never_cut():
    rows = build_keyboard("tx1", LIVE_CATEGORIES)
    shown = [b["text"] for r in rows for b in r]
    assert "Переказ рідним" in shown and "Переказ друзям" in shown


def test_a_label_too_wide_to_share_gets_a_row_of_its_own():
    rows = telegram_service._pack(["Кафе та ресторани", "Продукти"])
    assert rows[0] == ["Кафе та ресторани"]


def test_short_labels_still_pack_three_to_a_row():
    assert telegram_service._pack(["Краса", "Спорт", "Квіти"]) == \
        [["Краса", "Спорт", "Квіти"]]


def test_the_working_day_starts_at_nine_not_at_dawn():
    # The tagging loop re-asks its whole backlog the moment the cooldown
    # lapses, so the first run of the day is a burst of questions. At 05:00
    # that burst arrived before the owner was awake.
    assert main_module.ACTIVE_START_HOUR == 9


def test_the_digest_hour_is_inside_the_active_window():
    # A report scheduled outside the window is never sent: the run that would
    # send it exits before doing any API work.
    assert main_module.ACTIVE_START_HOUR <= main_module.DIGEST_HOUR \
        < main_module.ACTIVE_END_HOUR


# --- A model's guess is never applied without confirmation -------------------

def test_a_guessed_label_already_on_the_list_is_still_confirmed(monkeypatch):
    # The live incident: the owner typed "Кредит", the model answered
    # "Переказ рідним", and because that label was already on the list it was
    # written to the sheet with nothing on screen to catch it.
    monkeypatch.setattr(telegram_service.openrouter_client, 'propose_category',
                        lambda *a, **k: "Переказ рідним")
    client = FakeClient([reply_update(30, 102, "щось нерозпізнаване")])
    sheets = FakeSheets()
    state = tagging_state()
    pending = {"tx1": {"row": 7, "merchant": "На картку", "tx_id": "tx1"}}

    out = drain_answers(client, sheets, state, pending, ["Переказ рідним"])

    assert sheets.categories == [], "a guess was written without confirmation"
    assert out["proposed"]["tx1"] == "Переказ рідним"
    asked = client.sent[-1][1]
    assert "Записати" in asked and "здогадка" in asked


def test_a_locally_matched_answer_still_applies_without_a_confirm_step(monkeypatch):
    # Local matching is deterministic and reuses the parser's own vocabulary,
    # so it must stay a one-step answer - and must make no network call.
    def explode(*a, **k):
        raise AssertionError("a local match must never reach the model")
    monkeypatch.setattr(telegram_service.openrouter_client, 'propose_category', explode)
    client = FakeClient([reply_update(31, 102, "Кредит")])
    sheets = FakeSheets()
    pending = {"tx1": {"row": 7, "merchant": "На картку", "tx_id": "tx1"}}

    drain_answers(client, sheets, tagging_state(), pending, ["Кредит", "Продукти"])
    assert sheets.categories == [(7, "Кредит")]


# --- The watchdog: Telegram holding updates our poll never sees -------------
#
# The failure it exists for, observed live on 2026-08-14: the owner tapped a
# button, Telegram queued the update (pending_update_count went 0 -> 1), and
# ~3 minutes later the queue emptied on its own, with no run of ours in
# between. Every poll of ours returned []. Two separate incidents, both
# completely silent - the owner found them by noticing the sheet had not
# changed. An answer that is eaten must announce itself.

class WebhookClient:
    """A client that only answers the two calls the watchdog makes."""

    def __init__(self, pending, updates=()):
        self._pending = pending
        self._updates = list(updates)
        self.sent = []
        self.webhook_calls = 0

    def get_updates(self, offset):
        return self._updates

    def get_webhook_info(self):
        self.webhook_calls += 1
        return {"url": "", "pending_update_count": self._pending}

    def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))
        return {"message_id": 1}


def test_watchdog_fires_when_telegram_holds_updates_we_did_not_receive():
    client = WebhookClient(pending=2)
    state = empty_state(chat_id=500)
    state["asked"] = {"tx1": 1.0}
    text = telegram_service.check_undelivered(client, state, received=0, now=1000.0)
    assert text, 'an eaten answer must be reported'
    assert state["undelivered_alert_at"] == 1000.0


def test_watchdog_is_silent_when_we_received_the_updates_ourselves():
    # Telegram can queue an update between our getUpdates and this check; that
    # is an ordinary race, not the fault, so receiving anything at all clears.
    client = WebhookClient(pending=1)
    state = empty_state(chat_id=500)
    state["asked"] = {"tx1": 1.0}
    assert telegram_service.check_undelivered(
        client, state, received=1, now=1000.0) == ""
    assert client.webhook_calls == 0, 'no need to ask when the poll worked'


def test_watchdog_is_silent_with_an_empty_queue():
    client = WebhookClient(pending=0)
    state = empty_state(chat_id=500)
    state["asked"] = {"tx1": 1.0}
    assert telegram_service.check_undelivered(
        client, state, received=0, now=1000.0) == ""


def test_watchdog_is_silent_when_no_question_is_outstanding():
    # Nothing was asked, so nothing is owed: a stranger messaging the bot must
    # not page the owner.
    client = WebhookClient(pending=5)
    assert telegram_service.check_undelivered(
        client, empty_state(chat_id=500), received=0, now=1000.0) == ""
    assert client.webhook_calls == 0


def test_watchdog_does_not_repeat_inside_the_cooldown():
    client = WebhookClient(pending=2)
    state = empty_state(chat_id=500)
    state["asked"] = {"tx1": 1.0}
    first = telegram_service.check_undelivered(client, state, received=0, now=1000.0)
    inside = telegram_service.check_undelivered(client, state, received=0, now=1000.0 + 60)
    later = telegram_service.check_undelivered(
        client, state, received=0,
        now=1000.0 + telegram_service.UNDELIVERED_ALERT_COOLDOWN_HOURS * 3600 + 1)
    assert first and not inside and later


def test_watchdog_survives_a_telegram_outage():
    class Dead(WebhookClient):
        def get_webhook_info(self):
            return {}
    state = empty_state(chat_id=500)
    state["asked"] = {"tx1": 1.0}
    assert telegram_service.check_undelivered(
        Dead(pending=0), state, received=0, now=1000.0) == ""


def test_run_tagging_reports_an_eaten_answer_to_the_owner(monkeypatch, tmp_path):
    class Sheets(FakeSheets):
        def pending_rows(self):
            return [{"row": 5, "kind": "unknown", "merchant": "Cyberion",
                     "amount": 10, "tx_id": "tx1", "date": "", "time": ""}]

        def category_frequencies(self):
            return {}

    client = WebhookClient(pending=3)
    monkeypatch.setattr(telegram_service, 'TelegramClient', lambda token: client)
    state_path = str(tmp_path / "s.json")
    telegram_state.save_state(state_path, {**empty_state(chat_id=500),
                                           "asked": {"tx1": 9e9},
                                           "undelivered_alert_at": 0})
    run_tagging(Sheets(), "TOKEN", state_path)
    assert any('Telegram' in text for _, text in client.sent), client.sent
    # And the cooldown was persisted, so the next run does not repeat it.
    assert telegram_state.load_state(state_path)["undelivered_alert_at"] > 0


def test_scheduler_run_stands_down_when_the_worker_owns_the_bot(monkeypatch):
    # Two processes polling one bot token is the exact failure this project
    # spent two days chasing: whichever asks first gets the update, the other
    # gets a perfectly healthy empty batch, and an owner's answer disappears
    # with nothing logged anywhere. The long-polling worker is the production
    # consumer; when it is up, the Scheduler job must do nothing at all rather
    # than race it.
    called = []
    monkeypatch.setattr(main_module, '_run', lambda *a: called.append(1))
    monkeypatch.setattr(main_module, 'within_active_hours', lambda: True)
    monkeypatch.setenv('WORKER_ACTIVE', '1')
    main_module.main()
    assert called == [], "the scheduled run must stand down for the worker"


def test_scheduler_run_proceeds_when_no_worker_is_active(monkeypatch):
    called = []
    monkeypatch.setattr(main_module, '_run', lambda *a: called.append(1))
    monkeypatch.setattr(main_module, 'within_active_hours', lambda: True)
    monkeypatch.delenv('WORKER_ACTIVE', raising=False)
    main_module.main()
    assert called == [1]


def test_worker_calls_run_not_main_so_the_guard_cannot_stop_it():
    # WORKER_ACTIVE makes main.main() stand down. If the worker went through
    # main() too, setting that var would silence the whole pipeline instead of
    # just the Scheduler job - the guard would take down the thing it protects.
    import worker
    tree = ast.parse(inspect.getsource(worker))
    attrs = {f'{getattr(node.func.value, "id", "")}.{node.func.attr}'
             for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)}
    assert 'pipeline._run' in attrs
    assert 'pipeline.main' not in attrs


# --- Own-card transfers and payments by requisites --------------------------
#
# Both are rows the bank names, so the parser resolved them and the bot never
# asked, and both hide a purpose the sheet does not record. They are answered
# ONCE PER COUNTERPARTY - a card, a payee - not once per row, so one answer has
# to reach every queued row that shares it.

from categories import OWN_CARDS, REQUISITES
from telegram_service import CREDIT, choices_for, siblings_for


def own_card_pending(tx="own1", row=5, merchant="*1111", amount=6000):
    return {"row": row, "kind": "own_card", "merchant": merchant,
            "amount": amount, "tx_id": tx, "date": "14.08.2026", "time": "14:17"}


def test_an_own_card_question_offers_only_the_two_answers_that_differ():
    # Twenty-one buttons for a question with two real answers is worse than
    # useless: the two that matter are indistinguishable in the crowd.
    offered = choices_for(own_card_pending(), ["Продукти", "Транспорт"])
    assert offered == [CREDIT, OWN_CARDS]


def test_other_kinds_keep_the_full_category_list():
    row = {"kind": "requisites", "merchant": "ФОП Бондар"}
    assert choices_for(row, ["Продукти", "Транспорт"]) == ["Продукти", "Транспорт"]


def test_the_own_card_question_asks_what_actually_distinguishes_them():
    text = format_question(own_card_pending())
    assert 'кредит' in text.lower()


def test_the_requisites_question_asks_for_a_purpose():
    text = format_question({"kind": "requisites", "merchant": "ФОП Бондар",
                            "amount": 1423, "date": "", "time": ""})
    assert 'реквізит' in text.lower()


def test_siblings_are_every_queued_row_of_the_same_card():
    rows = {"a": own_card_pending("a", row=5, merchant="*1111"),
            "b": own_card_pending("b", row=9, merchant="*1111", amount=4000),
            "c": own_card_pending("c", row=11, merchant="*2222"),
            "d": {"row": 3, "kind": "unknown", "merchant": "*1111", "tx_id": "d"}}
    got = siblings_for(rows["a"], CREDIT, rows)
    assert sorted(r["tx_id"] for r in got) == ["b"], \
        'same card only, other kinds untouched, and never the row itself'


def test_answering_credit_turns_every_queued_row_of_that_card_into_spending():
    # The whole point of "once per card": the owner has three transfers to
    # *1111 sitting unexplained and must not be asked three times.
    client = FakeClient([callback_update(10, "a", CREDIT)])
    sheets = FakeSheets()
    pending = {"a": own_card_pending("a", row=5),
               "b": own_card_pending("b", row=9, amount=4000)}
    drain_answers(client, sheets, empty_state(chat_id=500), pending,
                  choices=["Продукти"])
    assert sorted(sheets.categories) == [(5, CREDIT), (9, CREDIT)]
    # Analytics drops a row by its TYPE, not its category, so the category
    # alone would leave this money invisible exactly as before.
    assert sorted(sheets.flows) == [(5, "Витрата"), (9, "Витрата")]


def test_answering_own_cards_records_the_answer_without_touching_the_type():
    # Genuinely internal: the money does come back, and turning it into an
    # expense would double-count every move between the owner's debit cards.
    client = FakeClient([callback_update(11, "a", OWN_CARDS)])
    sheets = FakeSheets()
    drain_answers(client, sheets, empty_state(chat_id=500),
                  {"a": own_card_pending("a", row=5, merchant="*2222")},
                  choices=["Продукти"])
    assert sheets.flows == [], 'an internal move must stay internal'
    assert ("*2222", OWN_CARDS) in sheets.rules, 'but it must never be asked again'


def test_a_card_rule_is_learned_even_though_a_card_tail_is_not_learnable():
    # is_learnable rejects "*1111" - four digits and no letters is exactly the
    # shape it exists to keep out of substring matching. The card rule is
    # written deliberately instead, because the tail is precisely what
    # apply_override matches on.
    assert not is_learnable("*1111")
    client = FakeClient([callback_update(12, "a", CREDIT)])
    sheets = FakeSheets()
    drain_answers(client, sheets, empty_state(chat_id=500),
                  {"a": own_card_pending("a")}, choices=["Продукти"])
    assert ("*1111", CREDIT) in sheets.rules


def test_an_own_card_answer_is_accepted_even_when_the_sheet_never_offers_it():
    # The acceptance check must read the same list the question offered, or a
    # button the bot itself drew is rejected on tap.
    client = FakeClient([callback_update(13, "a", OWN_CARDS)])
    sheets = FakeSheets()
    drain_answers(client, sheets, empty_state(chat_id=500),
                  {"a": own_card_pending("a")}, choices=["Продукти", "Таксі"])
    assert sheets.categories == [(5, OWN_CARDS)]


def test_one_answer_closes_every_queued_row_of_the_same_payee():
    # ФОП Бондар appears on five rows. Asking five times about the same payee
    # is exactly the noise that made the owner stop answering, and the rule the
    # first answer writes would silence the other four while leaving them
    # labelled "Платежі за реквізитами" forever.
    def payee(tx, row, merchant="ФОП БОНДАР ОЛЕНА ІВАНІВНА"):
        return {"row": row, "kind": "requisites", "merchant": merchant,
                "amount": 100, "tx_id": tx, "date": "", "time": ""}

    client = FakeClient([callback_update(20, "a", "Освіта")])
    sheets = FakeSheets()
    pending = {"a": payee("a", 5), "b": payee("b", 9),
               "c": payee("c", 12, "ФОП Савченко Ганна Іванівна")}
    drain_answers(client, sheets, empty_state(chat_id=500), pending,
                  choices=["Освіта"])
    assert sorted(sheets.categories) == [(5, "Освіта"), (9, "Освіта")]
    assert sheets.flows == [], 'a payment by requisites was already spending'


def test_siblings_are_exactly_the_rows_the_new_rule_will_silence():
    # The two must be the same set. Anything the rule covers but the answer
    # skips keeps its old label and is never asked about again - silently.
    row = {"row": 5, "kind": "requisites", "merchant": "ФОП Бондар Олена",
           "tx_id": "a"}
    others = {"a": row,
              "b": {"row": 9, "kind": "requisites",
                    "merchant": "ФОП БОНДАР ОЛЕНА ІВАНІВНА", "tx_id": "b"}}
    assert [r["tx_id"] for r in siblings_for(row, "Освіта", others)] == ["b"]


def test_one_question_per_counterparty_per_run():
    # 15 own-card rows across 4 cards were queued the moment this shipped, and
    # sorting by amount alone put five questions about *2222 in one batch -
    # the exact "answering the same question over and over" the whole design
    # was supposed to end. One answer closes the card, so one question is all
    # that can ever be useful.
    rows = []
    for index, (card, amount) in enumerate(
            [("*2222", 8000), ("*2222", 6000), ("*2222", 2000),
             ("*5555", 10351), ("*1111", 6000)]):
        rows.append({"row": index + 2, "kind": "own_card", "merchant": card,
                     "amount": amount, "tx_id": f'tx{index}',
                     "date": "01.01.2026", "time": "10:00"})
    client = FakeClient([])
    state = empty_state(chat_id=500)
    sent = ask_pending(client, 500, rows, state, ["Продукти"])
    asked = [text for _, text, keyboard in client.sent if keyboard]
    assert sent == 3, f'one per card, not one per row: {asked}'
    assert sum('*2222' in text for text in asked) == 1
    # And the one asked is the biggest, so the answer lands where it matters.
    assert any('8000' in text for text in asked)


def test_the_cooldown_is_per_counterparty_not_per_row():
    # Deduping within a run is not enough. The cooldown keys on tx_id, so the
    # next run skips the *2222 row it just asked about and promotes the
    # next-largest *2222 row into its place - and the owner is asked about the
    # same card again ten minutes later. One answer closes the card either way.
    def card_row(tx, row, amount, card="*2222"):
        return {"row": row, "kind": "own_card", "merchant": card,
                "amount": amount, "tx_id": tx, "date": "", "time": ""}

    rows = [card_row("big", 2, 8000), card_row("small", 3, 2000)]
    state = empty_state(chat_id=500)
    client = FakeClient([])
    assert ask_pending(client, 500, rows, state, ["Продукти"]) == 1
    assert "big" in state["asked"]

    # Immediately afterwards: the card is under cooldown, so nothing goes out.
    again = FakeClient([])
    assert ask_pending(again, 500, rows, state, ["Продукти"]) == 0, \
        'the other row of the same card must not become a fresh question'

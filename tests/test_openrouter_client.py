import json
import logging

import openrouter_client as oc


def model(mid, prompt="0", completion="0", inputs=("text",), outputs=("text",),
          context=100000):
    return {"id": mid,
            "pricing": {"prompt": prompt, "completion": completion},
            "architecture": {"input_modalities": list(inputs),
                             "output_modalities": list(outputs)},
            "context_length": context}


def fake_models(monkeypatch, models):
    payload = json.dumps({"data": models}).encode()
    monkeypatch.setattr(oc, "_get", lambda url, timeout: payload)


def test_paid_models_are_excluded(monkeypatch):
    fake_models(monkeypatch, [model("free:free"),
                              model("paid/x", prompt="0.000002")])
    assert "paid/x" not in oc.free_models()


def test_a_music_generator_is_excluded_despite_being_free(monkeypatch):
    # google/lyria-3-pro-preview is free and outputs audio. Without the
    # modality filter it is a legitimate candidate.
    fake_models(monkeypatch, [model("google/lyria-3-pro-preview",
                                    outputs=("text", "audio")),
                              model("ok/model:free")])
    assert oc.free_models() == ["ok/model:free"]


def test_a_vision_model_that_answers_in_text_is_kept(monkeypatch):
    fake_models(monkeypatch, [model("g/gemma:free", inputs=("image", "text"))])
    assert oc.free_models() == ["g/gemma:free"]


def test_classifiers_are_denylisted(monkeypatch):
    fake_models(monkeypatch, [model("nvidia/nemotron-3.5-content-safety:free"),
                              model("x/reranker:free"),
                              model("ok/model:free")])
    assert oc.free_models() == ["ok/model:free"]


def test_preferred_models_come_first_and_in_their_own_order(monkeypatch):
    fake_models(monkeypatch, [model("zzz/other:free", context=999999),
                              model(oc.PREFERRED[1]),
                              model(oc.PREFERRED[0])])
    ranked = oc.free_models()
    assert ranked[:2] == [oc.PREFERRED[0], oc.PREFERRED[1]]


def test_a_preferred_model_that_is_gone_is_not_offered(monkeypatch):
    fake_models(monkeypatch, [model("ok/model:free")])
    assert oc.PREFERRED[0] not in oc.free_models()


def test_the_rest_are_ordered_by_context_length(monkeypatch):
    fake_models(monkeypatch, [model("small:free", context=8000),
                              model("big:free", context=200000)])
    assert oc.free_models() == ["big:free", "small:free"]


def test_the_auto_router_is_the_last_resort(monkeypatch):
    fake_models(monkeypatch, [model(oc.LAST_RESORT, context=999999),
                              model("ok/model:free", context=1000)])
    assert oc.free_models()[-1] == oc.LAST_RESORT


def test_an_unreachable_list_falls_back_to_the_hard_coded_preferences(monkeypatch):
    def boom(url, timeout):
        raise OSError("network down")
    monkeypatch.setattr(oc, "_get", boom)
    assert oc.free_models() == list(oc.PREFERRED) + [oc.LAST_RESORT]


def test_malformed_json_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(oc, "_get", lambda url, timeout: b"{not json")
    assert oc.free_models() == list(oc.PREFERRED) + [oc.LAST_RESORT]


class FakeHTTP:
    """Replaces _post. `script` maps model id -> reply text or an Exception."""

    def __init__(self, script):
        self.script = script
        self.asked = []

    def __call__(self, url, body, headers, timeout):
        sent = json.loads(body.decode())
        self.asked.append(sent["model"])
        outcome = self.script.get(sent["model"], "стандартна відповідь")
        if isinstance(outcome, Exception):
            raise outcome
        return json.dumps({"choices": [{"message": {"content": outcome}}]}).encode()


class HTTPError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"HTTP {code}")


PAYLOAD = {"period": "month", "spend": 1000, "categories": []}


def test_the_first_model_that_answers_wins(monkeypatch):
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    http = FakeHTTP({"a:free": "аналіз від a"})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") == ("аналіз від a", "a:free")
    assert http.asked == ["a:free"]


def test_a_rate_limited_model_hands_over_to_the_next(monkeypatch):
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    http = FakeHTTP({"a:free": HTTPError(429), "b:free": "аналіз від b"})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") == ("аналіз від b", "b:free")
    assert http.asked == ["a:free", "b:free"]


def test_a_bad_key_stops_the_walk_immediately(monkeypatch):
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    http = FakeHTTP({"a:free": HTTPError(401), "b:free": "ніколи"})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") is None
    assert http.asked == ["a:free"]


def test_the_attempt_count_is_a_runaway_guard(monkeypatch):
    # Not the real ceiling - the wall-clock budget is - but a catalogue of
    # fourteen dead candidates should not be walked end to end either.
    fake_models(monkeypatch,
                [model(f"m{i}:free", context=100 - i) for i in range(20)])
    http = FakeHTTP({f"m{i}:free": HTTPError(503) for i in range(20)})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") is None
    assert len(http.asked) == oc.MAX_ATTEMPTS


def test_a_known_good_model_is_tried_first(monkeypatch):
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    http = FakeHTTP({})
    monkeypatch.setattr(oc, "_post", http)
    oc.analyze(PAYLOAD, "key", prefer="b:free")
    assert http.asked[0] == "b:free"


def test_a_stored_model_that_vanished_is_ignored(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    http = FakeHTTP({})
    monkeypatch.setattr(oc, "_post", http)
    oc.analyze(PAYLOAD, "key", prefer="gone/model:free")
    assert http.asked == ["a:free"]


def test_no_key_means_no_request_at_all(monkeypatch):
    http = FakeHTTP({})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "") is None
    assert http.asked == []


def test_an_empty_reply_is_treated_as_a_failure(monkeypatch):
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    http = FakeHTTP({"a:free": "   ", "b:free": "справжній аналіз"})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") == ("справжній аналіз", "b:free")


def test_an_overlong_reply_is_cut_at_a_sentence_end(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    long = ("Перше речення. " * 200) + "Останнє."
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": long}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert len(text) <= oc.MAX_ANALYSIS_CHARS
    assert text.endswith(".")


def test_the_api_key_never_reaches_a_log_line(monkeypatch, caplog):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": OSError("failed for sk-SECRET")}))
    with caplog.at_level(logging.DEBUG):
        oc.analyze(PAYLOAD, "sk-SECRET")
    assert "sk-SECRET" not in caplog.text


def test_the_key_travels_in_the_authorization_header(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    seen = {}

    def capture(url, body, headers, timeout):
        seen.update(headers)
        return json.dumps({"choices": [{"message": {"content": "ок"}}]}).encode()

    monkeypatch.setattr(oc, "_post", capture)
    oc.analyze(PAYLOAD, "sk-SECRET")
    assert seen["Authorization"] == "Bearer sk-SECRET"


def test_a_number_absent_from_the_input_is_logged_but_not_suppressed(monkeypatch, caplog):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "Ви витратили 999999 ₴."}))
    with caplog.at_level(logging.WARNING):
        text, _ = oc.analyze({"spend": 1000}, "key")
    assert "999999" in text          # the reader still gets the text
    assert "999999" in caplog.text   # but it is flagged


def test_a_unicode_thousands_separator_does_not_crash_the_check(monkeypatch):
    # A live model wrote "40 310" - a narrow no-break space. \s matched it
    # but str.replace(" ", "") did not, so float() raised and the exception
    # escaped analyze(), which must never raise.
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post",
                        FakeHTTP({"a:free": "Надійшло 40 310 ₴ за місяць."}))
    text, _ = oc.analyze({"income": 40310}, "key")
    assert "40" in text


def test_a_non_breaking_space_is_handled_too(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post",
                        FakeHTTP({"a:free": "Витрачено 32 780 ₴."}))
    assert oc.analyze({"spend": 32780}, "key") is not None


def test_the_number_check_can_never_break_the_answer(monkeypatch, caplog):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_known_numbers",
                        lambda payload: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "корисний текст"}))
    with caplog.at_level(logging.WARNING):
        result = oc.analyze({"spend": 1}, "key")
    assert result == ("корисний текст", "a:free")


def test_a_reply_cut_off_mid_sentence_is_trimmed_back(monkeypatch):
    # max_tokens stops the generation wherever it lands. The first live answer
    # ended "Цифрові товари +393 % (головне — під", which reads as a bug.
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post",
                        FakeHTTP({"a:free": "Перше речення. Друге обірване на пів"}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert text == "Перше речення."


def test_a_complete_reply_is_left_alone(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "Одне речення."}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert text == "Одне речення."


def test_a_reply_with_no_sentence_end_at_all_is_kept(monkeypatch):
    # Better a fragment than nothing; there is no boundary to cut back to.
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "суцільний фрагмент"}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert text == "суцільний фрагмент"


def test_a_complete_action_list_survives_intact(monkeypatch):
    # The prompt requires a full stop on every line, which is what makes a
    # missing one a reliable signal of truncation rather than of style.
    fake_models(monkeypatch, [model("a:free")])
    reply = ("Спостереження.\n\nЩо варто зробити:\n• Перше.\n• Друге.\n• Третє.")
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": reply}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert "• Третє." in text


def test_an_action_cut_off_mid_sentence_is_dropped_whole(monkeypatch):
    # Live: "• Розбити перекази «На картку» по категоріях, щоб звіти" - the
    # generation stopped there. A fragment of advice is worse than one action
    # fewer.
    fake_models(monkeypatch, [model("a:free")])
    reply = ("Спостереження.\n\nЩо варто зробити:\n• Перше.\n"
             "• Друге обірване на пів")
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": reply}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert "обірване" not in text
    assert "• Перше." in text


def test_a_reasoning_dump_is_rejected_and_the_next_model_is_tried(monkeypatch):
    # nemotron-3-ultra returned its English chain-of-thought instead of the
    # Ukrainian answer: "Let me analyze the data: Income: 40,310 | Spend..."
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    dump = ("Requirements:\n- No markdown\n- Max 120 words\n"
            "Let me analyze the data:\nIncome: 40310 | Spend: 32780\n"
            "Major issues: uncategorized spending is high")
    http = FakeHTTP({"a:free": dump, "b:free": "Липень вибився однією категорією."})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") == ("Липень вибився однією категорією.", "b:free")
    assert http.asked == ["a:free", "b:free"]


def test_ukrainian_prose_with_latin_merchant_names_is_accepted(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    reply = ("Кафе зросли через доставку: Glovo і Bolt Food разом дали більшу "
             "частину категорії, тоді як власні походи майже не змінились.")
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": reply}))
    assert oc.analyze(PAYLOAD, "key")[0] == reply


def test_the_request_asks_openrouter_to_omit_reasoning_tokens(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    seen = {}

    def capture(url, body, headers, timeout):
        seen.update(json.loads(body.decode()))
        return json.dumps({"choices": [{"message": {"content": "Текст українською."}}]}).encode()

    monkeypatch.setattr(oc, "_post", capture)
    oc.analyze(PAYLOAD, "key")
    assert seen["reasoning"] == {"exclude": True}


def test_the_walk_stops_when_the_wall_clock_budget_is_spent(monkeypatch):
    # urlopen's timeout is per-read, not total: a model that streams slowly
    # holds the connection far longer than TIMEOUT, so "3 attempts x 30s" was
    # never a real ceiling.
    fake_models(monkeypatch, [model(f"m{i}:free", context=100 - i) for i in range(3)])
    clock = {"now": 0.0}
    monkeypatch.setattr(oc.time, "monotonic", lambda: clock["now"])

    http = FakeHTTP({f"m{i}:free": HTTPError(503) for i in range(3)})
    original = http.__call__

    def slow(url, body, headers, timeout):
        clock["now"] += 100.0        # each attempt burns 100 seconds
        return original(url, body, headers, timeout)

    monkeypatch.setattr(oc, "_post", slow)
    assert oc.analyze(PAYLOAD, "key", budget=150) is None
    assert len(http.asked) == 2      # the third would start past the deadline


def test_a_fast_answer_is_unaffected_by_the_budget(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "Швидка відповідь."}))
    assert oc.analyze(PAYLOAD, "key", budget=1)[0] == "Швидка відповідь."


def test_the_per_call_timeout_never_exceeds_what_is_left(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    clock = {"now": 0.0}
    monkeypatch.setattr(oc.time, "monotonic", lambda: clock["now"])
    seen = {}

    def capture(url, body, headers, timeout):
        seen["timeout"] = timeout
        return json.dumps({"choices": [{"message": {"content": "Текст."}}]}).encode()

    monkeypatch.setattr(oc, "_post", capture)
    oc.analyze(PAYLOAD, "key", timeout=30, budget=10)
    assert seen["timeout"] <= 10


def test_an_empty_reply_is_logged_not_swallowed(monkeypatch, caplog):
    # A live run spent 130 seconds on a model and moved on without writing a
    # single line about it, which made the failure invisible.
    fake_models(monkeypatch, [model("a:free", context=3), model("b:free", context=2)])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "", "b:free": "Текст."}))
    with caplog.at_level(logging.INFO):
        oc.analyze(PAYLOAD, "key")
    assert "a:free" in caplog.text


def test_rate_limited_models_do_not_burn_the_whole_allowance(monkeypatch):
    # A 429 costs no time, so stopping after three of fourteen candidates threw
    # away every remaining chance for nothing. The wall clock is the real bound.
    fake_models(monkeypatch, [model(f"m{i}:free", context=100 - i) for i in range(6)])
    script = {f"m{i}:free": HTTPError(429) for i in range(5)}
    script["m5:free"] = "Знайшлася жива модель."
    http = FakeHTTP(script)
    monkeypatch.setattr(oc, "_post", http)
    assert oc.analyze(PAYLOAD, "key") == ("Знайшлася жива модель.", "m5:free")
    assert len(http.asked) == 6


def test_a_rejected_reply_is_logged_with_enough_to_judge_it(monkeypatch, caplog):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post",
                        FakeHTTP({"a:free": "Let me analyze the data carefully"}))
    with caplog.at_level(logging.INFO):
        oc.analyze(PAYLOAD, "key")
    assert "Let me analyze" in caplog.text


def test_complete_actions_survive_when_the_last_one_is_cut(monkeypatch):
    # Dropping the unfinished tail LINE threw away every action when the model
    # put them on one line. Cutting at the last sentence boundary keeps the
    # complete ones and loses only the fragment.
    fake_models(monkeypatch, [model("a:free")])
    reply = "Спостереження. Що варто зробити: • Перше. • Друге. • Третє обірване"
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": reply}))
    text, _ = oc.analyze(PAYLOAD, "key")
    assert "• Перше." in text and "• Друге." in text
    assert "обірване" not in text


import pytest

CHOICES = ["Продукти", "Таксі", "Ремонт техніки"]


def test_a_label_from_the_offered_list_is_returned(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "Таксі"}))
    assert oc.propose_category("їздив у сервіс", "Bolt", CHOICES, "key") == "Таксі"


def test_a_new_label_is_returned_as_proposed(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "Ремонт даху"}))
    assert oc.propose_category("платив за дах", "особа", CHOICES, "key") == "Ремонт даху"


def test_surrounding_chatter_is_stripped(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": "  Таксі  \n\nце найкраще"}))
    assert oc.propose_category("таксі", "Bolt", CHOICES, "key") == "Таксі"


@pytest.mark.parametrize("reply", ["", "   ", "Інше", "Не визначено",
                                   "Категорія з дуже довгою назвою що не влізе"])
def test_an_unusable_label_is_refused(monkeypatch, reply):
    fake_models(monkeypatch, [model("a:free")])
    monkeypatch.setattr(oc, "_post", FakeHTTP({"a:free": reply}))
    assert oc.propose_category("щось", "X", CHOICES, "key") is None


def test_no_key_means_no_proposal_request(monkeypatch):
    http = FakeHTTP({})
    monkeypatch.setattr(oc, "_post", http)
    assert oc.propose_category("щось", "X", CHOICES, "") is None
    assert http.asked == []


def test_the_counterparty_name_is_scrubbed_before_sending(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    seen = {}

    def capture(url, body, headers, timeout):
        seen["body"] = body.decode()
        return json.dumps({"choices": [{"message": {"content": "Ремонт"}}]}).encode()

    monkeypatch.setattr(oc, "_post", capture)
    oc.propose_category("за ремонт", "Петренко А. *3333", CHOICES, "key")
    assert "Петренко" not in seen["body"]
    assert "3333" not in seen["body"]


def test_the_text_is_truncated_before_it_becomes_a_prompt(monkeypatch):
    fake_models(monkeypatch, [model("a:free")])
    seen = {}

    def capture(url, body, headers, timeout):
        seen["body"] = body.decode()
        return json.dumps({"choices": [{"message": {"content": "Ремонт"}}]}).encode()

    monkeypatch.setattr(oc, "_post", capture)
    oc.propose_category("я" * 5000, "X", CHOICES, "key")
    # Counting occurrences would also count the ones in the system prompt;
    # what matters is that no run longer than the limit survives.
    assert "я" * (oc.MAX_TEXT_CHARS + 1) not in seen["body"]


# --- A model answering with prose instead of a label -------------------------

def test_a_prompt_echo_is_not_accepted_as_a_category():
    # Live incident: the model replied "The user wants to categorize a
    # transaction" - the system prompt paraphrased back. It was short enough to
    # pass every length check, was offered to the owner, confirmed, and became
    # a real category on a real row.
    from openrouter_client import _usable_label
    assert _usable_label("The user wants to categorize a transaction") is None


def test_an_english_label_is_rejected_but_a_ukrainian_one_is_kept():
    from openrouter_client import _usable_label
    assert _usable_label("Loan repayment") is None
    assert _usable_label("Погашення кредиту") == "Погашення кредиту"


def test_a_sentence_in_ukrainian_is_still_not_a_label():
    from openrouter_client import _usable_label
    assert _usable_label("Це схоже на переказ рідним за якісь послуги") is None


def test_a_three_word_category_survives():
    from openrouter_client import _usable_label
    assert _usable_label("Кафе та ресторани") == "Кафе та ресторани"

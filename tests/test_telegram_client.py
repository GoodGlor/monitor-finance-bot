import json
import logging

import telegram_client
from telegram_client import TelegramClient


def fake_transport(captured, response):
    def _post(url, body, timeout):
        captured.append((url, json.loads(body.decode())))
        return json.dumps(response).encode()
    return _post


def test_get_updates_sends_offset_and_returns_result(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": [{"update_id": 5}]}))
    got = TelegramClient("TOKEN").get_updates(3)
    assert got == [{"update_id": 5}]
    assert captured[0][1]["offset"] == 3


def test_token_is_never_logged_but_is_in_the_url(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": []}))
    TelegramClient("SECRET").get_updates(0)
    assert "SECRET" in captured[0][0]  # the URL must carry it


def test_network_failure_returns_empty_rather_than_raising(monkeypatch):
    def boom(url, body, timeout):
        raise OSError("network down")
    monkeypatch.setattr(telegram_client, '_post', boom)
    assert TelegramClient("TOKEN").get_updates(0) == []


def test_api_error_response_returns_none(monkeypatch):
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport([], {"ok": False, "description": "bad"}))
    assert TelegramClient("TOKEN").send_message(1, "hi") is None


def test_token_is_stripped_of_whitespace(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": []}))
    TelegramClient(" TOKEN \n").get_updates(0)
    assert captured[0][0] == f'{telegram_client.API_ROOT}TOKEN/getUpdates'


def test_token_never_appears_in_the_log_on_error(monkeypatch, caplog):
    # http.client.InvalidURL embeds the full request path - and therefore the
    # bot token - in its message even though the URL itself is never logged
    # directly. A token with whitespace or a control character can trigger it.
    def boom(url, body, timeout):
        raise ValueError(f'bad url: {telegram_client.API_ROOT}SECRET-TOKEN/getUpdates')
    monkeypatch.setattr(telegram_client, '_post', boom)
    with caplog.at_level(logging.WARNING):
        TelegramClient("SECRET-TOKEN").get_updates(0)
    assert "SECRET-TOKEN" not in caplog.text
    assert "ValueError" in caplog.text


def test_token_repr_escaped_form_is_also_scrubbed(monkeypatch, caplog):
    # http.client.InvalidURL formats the offending path with %r, so a token
    # containing a backslash appears repr-escaped (backslash doubled) in the
    # exception message - a literal str.replace(token, ...) would miss it.
    token = "SECRET\\TOKEN"     # one literal backslash in the raw token value
    escaped = repr(token)[1:-1]
    assert escaped != token    # sanity: repr really does differ here

    def boom(url, body, timeout):
        raise ValueError(f'bad url path: {escaped}')
    monkeypatch.setattr(telegram_client, '_post', boom)
    with caplog.at_level(logging.WARNING):
        TelegramClient(token).get_updates(0)
    assert token not in caplog.text
    assert escaped not in caplog.text
    assert "***" in caplog.text


def test_send_message_includes_keyboard_when_given(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": {"message_id": 1}}))
    TelegramClient("TOKEN").send_message(99, "text", [[{"text": "A", "callback_data": "a"}]])
    body = captured[0][1]
    assert body["chat_id"] == 99
    assert body["reply_markup"]["inline_keyboard"] == [[{"text": "A", "callback_data": "a"}]]


def test_answer_callback_failure_logs_at_debug_not_warning(monkeypatch, caplog):
    # A cron-driven bot always answers callbacks long after Telegram's short
    # validity window, so a failed answerCallbackQuery is expected noise, not
    # a fault worth a WARNING. Only this method should get the downgrade.
    def boom(url, body, timeout):
        raise OSError("network down")
    monkeypatch.setattr(telegram_client, '_post', boom)
    with caplog.at_level(logging.DEBUG):
        TelegramClient("TOKEN").answer_callback("cb1")
    assert any(r.levelno == logging.DEBUG and "answerCallbackQuery" in r.getMessage()
              for r in caplog.records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_get_updates_failure_logs_at_warning_not_debug(monkeypatch, caplog):
    # The DEBUG downgrade is specific to answerCallbackQuery; every other
    # method failing (e.g. a real outage during getUpdates) must stay visible.
    def boom(url, body, timeout):
        raise OSError("network down")
    monkeypatch.setattr(telegram_client, '_post', boom)
    with caplog.at_level(logging.DEBUG):
        TelegramClient("TOKEN").get_updates(0)
    assert any(r.levelno == logging.WARNING and "getUpdates" in r.getMessage()
              for r in caplog.records)
    assert not any(r.levelno == logging.DEBUG for r in caplog.records)


def test_token_never_appears_in_the_log_on_the_debug_path(monkeypatch, caplog):
    # The token-scrubbing logic runs before the level is chosen, but nothing
    # previously proved it also applies on the DEBUG (answerCallbackQuery)
    # branch rather than only the WARNING one exercised above.
    def boom(url, body, timeout):
        raise ValueError(
            f'bad url: {telegram_client.API_ROOT}SECRET-TOKEN/answerCallbackQuery')
    monkeypatch.setattr(telegram_client, '_post', boom)
    with caplog.at_level(logging.DEBUG):
        TelegramClient("SECRET-TOKEN").answer_callback("cb1")
    assert "SECRET-TOKEN" not in caplog.text
    assert "***" in caplog.text


def test_send_message_omits_parse_mode_by_default(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": {}}))
    TelegramClient("TOKEN").send_message(1, "hi")
    assert "parse_mode" not in captured[0][1]


def test_send_message_passes_parse_mode_when_given(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": {}}))
    TelegramClient("TOKEN").send_message(1, "hi", parse_mode="HTML")
    assert captured[0][1]["parse_mode"] == "HTML"


def test_get_webhook_info_returns_the_result_dict(monkeypatch):
    monkeypatch.setattr(telegram_client, '_post', fake_transport(
        [], {"ok": True, "result": {"url": "", "pending_update_count": 3}}))
    info = TelegramClient("TOKEN").get_webhook_info()
    assert info["pending_update_count"] == 3


def test_get_webhook_info_degrades_to_an_empty_dict(monkeypatch):
    # Same contract as every other method here: a Telegram outage must never
    # raise into the pipeline. An empty dict reads as "nothing known", which
    # the caller treats as "no anomaly" rather than crying wolf.
    def boom(url, body, timeout):
        raise OSError("network down")
    monkeypatch.setattr(telegram_client, '_post', boom)
    assert TelegramClient("TOKEN").get_webhook_info() == {}


def test_get_webhook_info_does_not_consume_updates(monkeypatch):
    # The whole point of using this endpoint for the watchdog: getUpdates with
    # an offset permanently forgets everything below it, so the watchdog must
    # not be another getUpdates caller.
    captured = []
    monkeypatch.setattr(telegram_client, '_post',
                        fake_transport(captured, {"ok": True, "result": {}}))
    TelegramClient("TOKEN").get_webhook_info()
    assert captured[0][0].endswith('/getWebhookInfo')
    assert captured[0][1] == {}

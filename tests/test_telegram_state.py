import json

import pytest

from telegram_state import load_state, parse_state, save_state, serialize_state

DEFAULTS = {"offset": 0, "chat_id": None, "asked": {}, "declined": [],
            "replies": {}, "proposed": {}, "ask_count": {},
            "undelivered_alert_at": 0}


def test_missing_file_returns_defaults(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == DEFAULTS


def test_round_trip(tmp_path):
    p = str(tmp_path / "s.json")
    save_state(p, dict(DEFAULTS, offset=42, chat_id=12345,
                       asked={"tx1": 100.0}, declined=["tx2"]))
    assert load_state(p) == dict(DEFAULTS, offset=42, chat_id=12345,
                                 asked={"tx1": 100.0}, declined=["tx2"])


def test_save_state_missing_ledger_raises(tmp_path):
    # A state dict missing a ledger must fail loudly rather than silently
    # erasing it - the reason the old signature took no defaults.
    p = str(tmp_path / "s.json")
    with pytest.raises(KeyError):
        save_state(p, {"offset": 42, "chat_id": 12345})


def test_corrupt_file_returns_defaults_rather_than_raising(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert load_state(str(p)) == DEFAULTS


def test_partial_file_is_filled_with_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"offset": 7}))
    assert load_state(str(p)) == dict(DEFAULTS, offset=7)


@pytest.mark.parametrize("content", ["42", "null", "true", '"offset in me"', "[1, 2, 3]"])
def test_non_object_json_returns_defaults(tmp_path, content):
    p = tmp_path / "s.json"
    p.write_text(content)
    assert load_state(str(p)) == DEFAULTS


def test_binary_file_returns_defaults(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(b"\xff\xfe\x00binary")
    assert load_state(str(p)) == DEFAULTS


def test_string_offset_is_coerced_to_int(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"offset": "5"}))
    assert load_state(str(p))["offset"] == 5


def test_unparseable_offset_falls_back_to_default(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"offset": "not-a-number"}))
    assert load_state(str(p))["offset"] == 0


def test_wrong_type_asked_falls_back_to_default(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"asked": "not-a-dict"}))
    assert load_state(str(p))["asked"] == {}


def test_wrong_type_declined_falls_back_to_default(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"declined": {"not": "a list"}}))
    assert load_state(str(p))["declined"] == []


def test_asked_entry_with_non_numeric_timestamp_is_dropped(tmp_path):
    # A hand-edited non-numeric timestamp must not survive load: `now -
    # asked[tx_id]` in ask_pending would raise TypeError on every subsequent
    # run, silently disabling tagging.
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"asked": {"bad": "not-a-number", "good": 123.0}}))
    assert load_state(str(p))["asked"] == {"good": 123.0}


def test_declined_entry_with_non_string_element_is_dropped(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"declined": ["tx1", 42, None, "tx2"]}))
    assert load_state(str(p))["declined"] == ["tx1", "tx2"]


def test_defaults_are_not_shared_between_calls(tmp_path):
    a = load_state(str(tmp_path / "nope.json"))
    a["asked"]["x"] = 1
    a["declined"].append("y")
    b = load_state(str(tmp_path / "nope-again.json"))
    assert b["asked"] == {}
    assert b["declined"] == []


# --- stores ------------------------------------------------------------------


class FakeSheets:
    def __init__(self, cell=None):
        self.cell = cell
        self.writes = []

    def read_state(self):
        return self.cell

    def write_state(self, text):
        self.writes.append(text)
        self.cell = text


def test_parse_state_of_none_is_defaults():
    from telegram_state import parse_state
    assert parse_state(None) == DEFAULTS


def test_sheet_store_round_trips():
    from telegram_state import SheetStore, parse_state, serialize_state

    sheets = FakeSheets()
    store = SheetStore(sheets, legacy_path="/nonexistent/state.json")
    store.write(serialize_state(dict(DEFAULTS, offset=9, chat_id=500,
                                     asked={"a": 1.0}, declined=["b"])))

    assert parse_state(store.read()) == dict(DEFAULTS, offset=9, chat_id=500,
                                            asked={"a": 1.0}, declined=["b"])


def test_sheet_store_seeds_from_the_legacy_file_when_the_cell_is_empty(tmp_path):
    # Without this, the first run after moving to sheet-backed state would
    # discard the owner's declined ledger and re-ask every declined row.
    from telegram_state import SheetStore, parse_state

    legacy = tmp_path / "s.json"
    legacy.write_text(json.dumps({"offset": 4, "chat_id": 77,
                                  "asked": {}, "declined": ["old"]}))

    store = SheetStore(FakeSheets(cell=None), legacy_path=str(legacy))
    assert parse_state(store.read())["declined"] == ["old"]


def test_sheet_store_prefers_the_sheet_over_the_legacy_file(tmp_path):
    from telegram_state import SheetStore, parse_state

    legacy = tmp_path / "s.json"
    legacy.write_text(json.dumps({"offset": 4, "declined": ["stale"]}))

    sheets = FakeSheets(cell=json.dumps({"offset": 99, "declined": ["current"]}))
    store = SheetStore(sheets, legacy_path=str(legacy))
    state = parse_state(store.read())
    assert state["offset"] == 99 and state["declined"] == ["current"]


def test_sheet_store_survives_a_host_with_no_legacy_file():
    from telegram_state import SheetStore, parse_state

    store = SheetStore(FakeSheets(cell=None), legacy_path="/nonexistent/state.json")
    assert parse_state(store.read()) == DEFAULTS


FULL = {"offset": 4, "chat_id": 9, "asked": {"t": 1.0}, "declined": ["d"],
        "replies": {"77": "tx1"}, "proposed": {"tx1": "Ремонт"},
        "ask_count": {"tx1": 2}, "undelivered_alert_at": 1786700000.0}


def test_defaults_include_every_ledger():
    assert load_state("/nonexistent") == {
        "offset": 0, "chat_id": None, "asked": {}, "declined": [],
        "replies": {}, "proposed": {}, "ask_count": {},
        "undelivered_alert_at": 0}


def test_round_trip_keeps_replies_and_proposals():
    assert parse_state(serialize_state(FULL)) == FULL


def test_a_message_id_stays_a_string_through_a_round_trip():
    # JSON object keys are strings. An int key would come back as "77" and
    # every lookup after a save would miss.
    got = parse_state(serialize_state(dict(FULL, replies={77: "tx1"})))
    assert got["replies"] == {"77": "tx1"}


def test_serialize_raises_when_a_ledger_is_missing():
    # The old signature took every value positionally with no defaults so a
    # forgetful caller failed loudly instead of erasing a ledger. Indexing the
    # whitelist keeps exactly that property.
    with pytest.raises(KeyError):
        serialize_state({"offset": 1, "chat_id": None})


def test_a_malformed_replies_value_degrades_to_empty():
    assert parse_state(json.dumps({"replies": "not a map"}))["replies"] == {}
    assert parse_state(json.dumps({"proposed": [1, 2]}))["proposed"] == {}

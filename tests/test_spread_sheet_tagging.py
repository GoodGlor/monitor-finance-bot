from datetime import datetime

from categories import UNKNOWN
from spread_sheet_service import SpreadSheetService


class FakeValues:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []
        self.appends = []

    def get(self, spreadsheetId=None, range=None, **kw):
        rows = self._rows
        return self._Exec({"values": rows})

    def update(self, spreadsheetId=None, range=None, body=None, **kw):
        self.updates.append((range, body))
        return self._Exec({})

    def append(self, spreadsheetId=None, range=None, body=None, **kw):
        self.appends.append((range, body))
        return self._Exec({})

    class _Exec:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload


def build(rows, rules=None):
    fake = FakeValues(rows)
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = fake
    svc.spreadsheet_id = "SHEET"
    svc.rules = dict(rules) if rules else {}
    return svc, fake


class RangeAwareValues:
    def __init__(self, by_range):
        self._by_range = by_range

    def get(self, spreadsheetId=None, range=None, **kw):
        return self._Exec({"values": self._by_range.get(range, [])})

    class _Exec:
        def __init__(self, payload):
            self._payload = payload

        def execute(self):
            return self._payload


def build_ranged(by_range):
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = RangeAwareValues(by_range)
    svc.spreadsheet_id = "SHEET"
    return svc


def test_pending_rows_finds_only_unresolved():
    rows = [
        [46204, 0.5, "Витрата", "Продукти", "Сільпо", 100, 500, "aaa"],
        [46205, 0.5, "Витрата", UNKNOWN, "На картку", 250, 250, "bbb"],
    ]
    svc, _ = build(rows)
    pending = svc.pending_rows()
    assert len(pending) == 1
    assert pending[0]["tx_id"] == "bbb"
    assert pending[0]["row"] == 3          # header is row 1, data starts at row 2


def test_set_category_writes_only_column_D():
    svc, fake = build([])
    svc.set_category(7, "Комуналка")
    range_used, body = fake.updates[0]
    assert range_used == "data!D7"
    assert body["values"] == [["Комуналка"]]


def test_append_rule_targets_sheet2():
    svc, fake = build([])
    svc.append_rule("Cyberion", "Інтернет")
    range_used, body = fake.appends[0]
    assert range_used.startswith("Sheet2!")
    assert body["values"] == [["Cyberion", "Інтернет"]]
    assert svc.rules["Cyberion"] == "Інтернет"


def test_append_rule_skips_duplicate_key_already_loaded():
    svc, fake = build([], rules={"Cyberion": "Інтернет"})
    svc.append_rule("Cyberion", "Продукти")
    assert fake.appends == []


def test_append_rule_skips_duplicate_written_earlier_this_run():
    svc, fake = build([])
    svc.append_rule("Cyberion", "Інтернет")
    svc.append_rule("Cyberion", "Інтернет")
    assert len(fake.appends) == 1


def test_category_frequencies_counts_resolved_rows():
    rows = [
        ["d", "t", "Витрата", "Продукти", "Сільпо", 1, 2, "a"],
        ["d", "t", "Витрата", "Продукти", "АТБ", 1, 2, "b"],
        ["d", "t", "Витрата", "Таксі", "Bolt", 1, 2, "c"],
        ["d", "t", "Витрата", UNKNOWN, "На картку", 1, 2, "d"],
    ]
    svc, _ = build(rows)
    freq = svc.category_frequencies()
    assert freq["Продукти"] == 2
    assert freq["Таксі"] == 1
    assert UNKNOWN not in freq          # unresolved rows are not a "common answer"


def test_button_categories_reads_the_tab_in_order():
    svc = build_ranged({"Категорії!A2:A": [["Комуналка"], ["Квартплата"], ["Продукти"]]})
    assert svc.button_categories() == ["Комуналка", "Квартплата", "Продукти"]


def test_button_categories_strips_blanks_and_duplicates():
    svc = build_ranged({"Категорії!A2:A": [["  Продукти  "], [""], ["Продукти"], []]})
    assert svc.button_categories() == ["Продукти"]


def test_button_categories_returns_empty_when_tab_absent():
    class Boom:
        def get(self, **kw):
            raise RuntimeError("Unable to parse range")
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = Boom()
    svc.spreadsheet_id = "SHEET"
    assert svc.button_categories() == []


# --- Telegram state stored in the sheet -------------------------------------
#
# The pipeline must survive a host with an ephemeral filesystem, where every
# run starts from a fresh slug and anything written to disk is discarded.


class StateValues:
    """A values() fake whose A1 cell can be empty, absent, or preloaded."""

    def __init__(self, cell=None, update_error=None):
        self.cell = cell
        self.updates = []
        self._update_error = update_error

    def get(self, spreadsheetId=None, range=None, **kw):
        values = [[self.cell]] if self.cell is not None else []
        return _Exec({"values": values})

    def update(self, spreadsheetId=None, range=None, body=None, **kw):
        if self._update_error is not None:
            error, self._update_error = self._update_error, None
            raise error
        self.updates.append((range, body, kw.get("valueInputOption")))
        self.cell = body["values"][0][0]
        return _Exec({})


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values
        self.batch_updates = []

    def values(self):
        return self._values

    def batchUpdate(self, spreadsheetId=None, body=None, **kw):
        self.batch_updates.append(body)
        return _Exec({})


def build_state_svc(cell=None, update_error=None):
    values = StateValues(cell, update_error)
    parent = FakeSpreadsheets(values)
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc._spreadsheets = parent
    svc.sheet_service = values
    svc.spreadsheet_id = "SHEET"
    return svc, values, parent


def test_read_state_returns_none_when_the_cell_is_empty():
    svc, _, _ = build_state_svc()
    assert svc.read_state() is None


def test_read_state_returns_none_when_the_tab_is_missing():
    # A missing tab makes the API raise; that is the ordinary first-run case
    # and must degrade to defaults rather than crash the cron run.
    class Exploding:
        def get(self, **kw):
            raise RuntimeError("Unable to parse range: _state!A1")

    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = Exploding()
    svc.spreadsheet_id = "SHEET"
    assert svc.read_state() is None


def test_read_state_ignores_a_non_string_cell():
    svc, _, _ = build_state_svc(cell=42)
    assert svc.read_state() is None


def test_write_state_round_trips():
    svc, values, _ = build_state_svc()
    svc.write_state('{"offset": 7}')
    assert svc.read_state() == '{"offset": 7}'
    # RAW, not USER_ENTERED: a payload opening with "{" must not be interpreted.
    assert values.updates[0][2] == "RAW"


def test_write_state_creates_the_tab_then_retries():
    from googleapiclient.errors import HttpError

    class Resp:
        status = 400
        reason = "Bad Request"

    svc, values, parent = build_state_svc(
        update_error=HttpError(Resp(), b'{"error": {"message": "Unable to parse range"}}'))
    svc.write_state('{"offset": 7}')

    titles = [r["addSheet"]["properties"]["title"]
              for body in parent.batch_updates for r in body["requests"]]
    assert titles == ["_state"]
    # The retry must actually land - a state write that silently fails means
    # `asked` is never persisted and every pending row is re-asked every run.
    assert svc.read_state() == '{"offset": 7}'


# --- newest transaction ------------------------------------------------------


def _svc_with_rows(rows):
    values = StateValues()
    values.get = lambda spreadsheetId=None, range=None, **kw: _Exec({"values": rows})
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = values
    svc.spreadsheet_id = "SHEET"
    return svc


def test_latest_transaction_at_converts_the_sheets_serial():
    from datetime import datetime
    # 46231 + 0.2777... is 2026-07-28 06:40 (Sheets counts days from 1899-12-30).
    svc = _svc_with_rows([[46231, 0.2777777777777778]])
    assert svc.latest_transaction_at() == datetime(2026, 7, 28, 6, 40)


def test_latest_transaction_at_takes_the_max_not_the_last_row():
    from datetime import datetime
    # Real data is not sorted: the live sheet has 7/24 17:30 above 7/24 13:58,
    # so reading the bottom row would understate how fresh the feed is.
    svc = _svc_with_rows([[46231, 0.5], [46230, 0.9], [46229, 0.1]])
    assert svc.latest_transaction_at() == datetime(2026, 7, 28, 12, 0)


def test_latest_transaction_at_handles_a_missing_time():
    from datetime import datetime
    svc = _svc_with_rows([[46231]])
    assert svc.latest_transaction_at() == datetime(2026, 7, 28, 0, 0)


def test_latest_transaction_at_skips_text_rows():
    from datetime import datetime
    svc = _svc_with_rows([["Date", "Time"], [], [46231, 0.5], ["oops"]])
    assert svc.latest_transaction_at() == datetime(2026, 7, 28, 12, 0)


def test_latest_transaction_at_is_none_for_an_empty_sheet():
    assert _svc_with_rows([]).latest_transaction_at() is None


def test_latest_transaction_at_is_none_when_the_read_fails():
    # Must degrade to "unknown", which the caller treats as not-stale, rather
    # than raising inside an otherwise-successful run.
    class Exploding:
        def get(self, **kw):
            raise RuntimeError("Sheets 500")

    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = Exploding()
    svc.spreadsheet_id = "SHEET"
    assert svc.latest_transaction_at() is None


# --- all_rows for digest ---------------------------------------------------


def test_all_rows_converts_serials_to_datetimes():
    # 46218 = 2026-07-15; 0.5 = midday
    svc, _ = build([[46218, 0.5, "Витрата", "Продукти", "Сільпо", 250.5, 1000, "t1"]])
    rows = svc.all_rows()
    assert rows == [{"date": datetime(2026, 7, 15, 12, 0),
                     "kind": "Витрата", "category": "Продукти",
                     "merchant": "Сільпо", "amount": 250.5,
                     "balance": 1000.0}]


def test_all_rows_skips_short_rows_and_non_numeric_dates():
    svc, _ = build([
        [46218, 0.5, "Витрата", "Продукти", "Сільпо", 250.5, 1000, "t1"],
        [46218, 0.5, "Витрата", "Продукти"],                      # too short
        ["15/07/2026", 0.5, "Витрата", "Продукти", "X", 10, 0, "t2"],  # text date
        [46218, 0.5, "Витрата", "Продукти", "X", "нема", 0, "t3"],     # text amount
    ])
    assert [r["merchant"] for r in svc.all_rows()] == ["Сільпо"]


def test_all_rows_tolerates_a_missing_time_cell():
    svc, _ = build([[46218, "", "Витрата", "Продукти", "Сільпо", 250.5, 1000, "t1"]])
    assert svc.all_rows()[0]["date"] == datetime(2026, 7, 15, 0, 0)


def test_all_rows_rejects_booleans_which_python_counts_as_numbers():
    svc, _ = build([[True, 0.5, "Витрата", "Продукти", "X", 10, 0, "t1"]])
    assert svc.all_rows() == []


def test_state_cell_defaults_to_a1_and_can_be_overridden():
    svc, fake = build([])
    svc.write_state("{}")
    svc.write_state("{}", cell=svc.DIGEST_STATE_RANGE)
    assert [rng for rng, _ in fake.updates] == ['_state!A1', '_state!A2']


def test_all_rows_carries_the_balance_column():
    svc, _ = build([[46218, 0.5, "Витрата", "Продукти", "Сільпо", 250.5, 1000.25, "t1"]])
    assert svc.all_rows()[0]["balance"] == 1000.25


def test_a_missing_or_textual_balance_becomes_none_rather_than_zero():
    # Zero would read as an empty card; None reads as "not known".
    svc, _ = build([[46218, 0.5, "Витрата", "Продукти", "Сільпо", 250.5, "", "t1"]])
    assert svc.all_rows()[0]["balance"] is None


P2P = "Перекази — людям / P2P"
# 46231 = 2026-07-28 (older than the cutoff), 46246 = 2026-08-12 (newer)
OLD, NEW = 46231, 46246


def transfer_row(serial, amount, tx_id, category=P2P):
    return [serial, 0.5, "Витрата", category, "Петренко А.", amount, 1000, tx_id]


def test_a_large_historical_transfer_is_queued():
    # 90 383 UAH of transfers carried no purpose and were never asked about,
    # because the parser HAD categorised them.
    svc, _ = build([transfer_row(OLD, 7500, "big")])
    assert [r["tx_id"] for r in svc.pending_rows()] == ["big"]


def test_a_small_historical_transfer_is_left_alone():
    # The owner cannot recall small transfers from months ago; asking about
    # them would produce guesses, which are worse than a blank.
    svc, _ = build([transfer_row(OLD, 200, "small")])
    assert svc.pending_rows() == []


def test_every_new_transfer_is_queued_whatever_its_size():
    svc, _ = build([transfer_row(NEW, 200, "fresh")])
    assert [r["tx_id"] for r in svc.pending_rows()] == ["fresh"]


def test_transfers_are_marked_apart_from_uncategorised_rows():
    svc, _ = build([
        [OLD, 0.5, "Витрата", UNKNOWN, "На картку", 250, 250, "u"],
        transfer_row(OLD, 7500, "t"),
    ])
    kinds = {r["tx_id"]: r["kind"] for r in svc.pending_rows()}
    assert kinds == {"u": "unknown", "t": "transfer"}


def test_an_outgoing_own_card_transfer_is_asked_about_once():
    # This test used to assert the opposite - "money never left, there is no
    # purpose to record". That held only while every own card was a debit
    # card. *1111 is a credit card: 10 005 UAH went to it across three
    # transfers with no matching return, and repaying a credit is real
    # spending that no other row records. One question per card, and the rule
    # the answer writes is what stops the second one.
    svc, _ = build([[NEW, 0.5, "Переказ", "Перекази — свої картки",
                     "*3333", 9000, 1000, "own"]])
    assert [r["kind"] for r in svc.pending_rows()] == ["own_card"]


def test_ordinary_spending_is_still_not_queued():
    svc, _ = build([[NEW, 0.5, "Витрата", "Продукти", "Сільпо", 100, 500, "s"]])
    assert svc.pending_rows() == []


def test_the_question_date_does_not_depend_on_the_sheet_locale():
    svc, _ = build([transfer_row(OLD, 7500, "big")])
    assert svc.pending_rows()[0]["date"] == "28.07.2026"
    assert svc.pending_rows()[0]["time"] == "12:00"


def test_direction_picks_the_question_not_whether_to_ask():
    # Both are asked about, but "what was it for" and "where did it come from"
    # are different questions, and the category label cannot tell them apart -
    # an incoming "Зарахування переказу" carries the same one.
    svc, _ = build([
        [NEW, 0.5, "Дохід", P2P, "Зарахування", 9000, 1000, "in"],
        [NEW, 0.5, "Витрата", P2P, "Петренко А.", 9000, 1000, "out"],
    ])
    kinds = {r["tx_id"]: r["kind"] for r in svc.pending_rows()}
    assert kinds == {"in": "incoming", "out": "transfer"}


def test_cash_withdrawals_are_queued_too():
    # 23 000 UAH left as cash with no record of where it went.
    svc, _ = build([[OLD, 0.5, "Витрата", "Зняття готівки",
                     "ХРЕЩАТИК, БУД. 1", 2500, 1000, "atm"]])
    assert [r["kind"] for r in svc.pending_rows()] == ["cash"]


def test_incoming_transfers_are_queued_as_their_own_kind():
    # Not "what was it spent on" but "where did it come from".
    svc, _ = build([[OLD, 0.5, "Дохід", P2P, "Коваль Ю.", 1500, 1000, "in"]])
    assert [r["kind"] for r in svc.pending_rows()] == ["incoming"]


def test_the_size_rule_applies_to_every_movement_kind():
    svc, _ = build([
        [OLD, 0.5, "Витрата", "Зняття готівки", "Банкомат", 200, 1000, "old_small"],
        [NEW, 0.5, "Витрата", "Зняття готівки", "Банкомат", 200, 1000, "new_small"],
        [OLD, 0.5, "Дохід", P2P, "Хтось", 300, 1000, "old_small_in"],
    ])
    assert [r["tx_id"] for r in svc.pending_rows()] == ["new_small"]


def test_an_answered_own_card_goes_quiet_for_good():
    # The counterpart of the test above: asked once, then never again. Without
    # this the owner would be asked about every single transfer between their
    # own cards, which is most of them.
    rows = [[NEW, 0.5, "Переказ", "Перекази — свої картки",
             "*3333", 9000, 1000, "own"]]
    assert build(rows)[0].pending_rows(), 'unanswered: must ask'
    svc, _ = build(rows, rules={"*3333": "Перекази — свої картки"})
    assert svc.pending_rows() == [], "answered: must stay quiet"


def test_salary_is_not_mistaken_for_a_movement():
    svc, _ = build([[NEW, 0.5, "Дохід", "Зарплата", "ТОВ", 40000, 1000, "pay"]])
    assert svc.pending_rows() == []


class ButtonSheets:
    """Fake whose Категорії tab can be read and appended to."""

    def __init__(self, labels):
        self.labels = list(labels)
        self.appends = []

    def get(self, spreadsheetId=None, range=None, **kw):
        return _Exec({"values": [[label] for label in self.labels]})

    def append(self, spreadsheetId=None, range=None, body=None, **kw):
        self.appends.append((range, body))
        self.labels.append(body["values"][0][0])
        return _Exec({})


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def build_buttons(labels):
    fake = ButtonSheets(labels)
    svc = SpreadSheetService.__new__(SpreadSheetService)
    svc.sheet_service = fake
    svc.spreadsheet_id = "SHEET"
    svc.rules = {}
    return svc, fake


def test_a_new_button_category_is_appended_to_the_tab():
    svc, fake = build_buttons(["Продукти"])
    assert svc.append_button_category("Ремонт") is True
    assert fake.appends[0][1]["values"] == [["Ремонт"]]


def test_a_button_category_already_present_is_not_appended_twice():
    svc, fake = build_buttons(["Продукти"])
    assert svc.append_button_category("Продукти") is False
    assert fake.appends == []


def test_the_button_category_check_ignores_case_and_padding():
    svc, fake = build_buttons(["Продукти"])
    assert svc.append_button_category("  продукти ") is False
    assert fake.appends == []


def test_an_empty_button_category_is_refused():
    svc, fake = build_buttons(["Продукти"])
    assert svc.append_button_category("   ") is False
    assert fake.appends == []


# --- Movements the bank names, so the parser resolved them and nobody asked --
#
# Two more classes of row carry a category and no purpose. A transfer to the
# owner's own credit card is not an internal move at all - the money leaves and
# never comes back, and *1111 shows exactly that asymmetry: 10 005 UAH out
# across three transfers and not one matching return. A payment by requisites
# records a name in the merchant column and nothing about what was bought.

from categories import OWN_CARDS, REQUISITES
from spread_sheet_service import SHEETS_EPOCH, TRANSFER_MIN_UAH


def _serial(when):
    return (when - SHEETS_EPOCH).days


RECENT = _serial(datetime(2026, 8, 14))


def own_card_row(merchant="*1111", amount=6000, tx="tx-own"):
    return [RECENT, 0.6, "Переказ", OWN_CARDS, merchant, amount, 100, tx]


def requisites_row(merchant="ФОП БОНДАР ОЛЕНА ІВАНІВНА", amount=1423,
                   tx="tx-req"):
    return [RECENT, 0.6, "Витрата", REQUISITES, merchant, amount, 100, tx]


def test_an_outgoing_transfer_to_an_own_card_is_queued():
    svc, _ = build([own_card_row()])
    pending = svc.pending_rows()
    assert [row["kind"] for row in pending] == ["own_card"]


def test_an_incoming_own_card_transfer_is_not_queued():
    # The other leg of a genuine internal move. Asking about both halves of the
    # same money is asking the same question twice.
    svc, _ = build([own_card_row(merchant="Переказ зі своєї картки *2222")])
    assert svc.pending_rows() == []


def test_an_own_card_with_a_rule_is_never_asked_about_again():
    # The rule IS the memory of "you already told me about this card". One
    # answer per card, not one per transfer.
    svc, _ = build([own_card_row()], rules={"*1111": "Кредит"})
    assert svc.pending_rows() == []


def test_a_small_historical_own_card_transfer_stays_quiet():
    old = own_card_row(amount=5)
    old[0] = _serial(datetime(2026, 7, 28))
    svc, _ = build([old])
    assert svc.pending_rows() == [], 'a 5 UAH transfer is not worth a question'
    assert TRANSFER_MIN_UAH > 5


def test_payments_by_requisites_are_queued_whatever_the_amount():
    # The owner asked for all of them, old ones included: there are only about
    # two a month, and 8 758 UAH of them carried no purpose at all.
    svc, _ = build([requisites_row(amount=23, tx="a"),
                    requisites_row(amount=1423, tx="b")])
    assert [row["kind"] for row in svc.pending_rows()] == ["requisites",
                                                           "requisites"]


def test_a_requisites_payee_with_a_rule_is_not_asked_about_again():
    svc, _ = build([requisites_row()], rules={"БОНДАР": "Освіта"})
    assert svc.pending_rows() == []

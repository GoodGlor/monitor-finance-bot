from auth_service import AuthService
from categories import (
    CASH, EXPENSE, INTERNAL, OWN_CARDS, REQUISITES, TRANSFERS_OUT, UNKNOWN,
    apply_override,
)
from googleapiclient.errors import HttpError
from datetime import date, datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# Google Sheets counts days from 1899-12-30.
SHEETS_EPOCH = datetime(1899, 12, 30)

# Transfers below this are only asked about if they are recent: the owner can
# answer for the large historical ones and not for the rest, and a guessed
# category is worse than a blank.
# A sentinel that cannot equal any real category, so "did a rule fire?" is an
# identity check rather than a comparison that a rule returning the same
# category would answer wrongly.
_NO_RULE = object()

TRANSFER_MIN_UAH = 1000.0
TRANSFER_ASK_ALL_FROM = date(2026, 8, 8)


def _moment(serial, fraction):
    """A cell pair as a datetime, or None when the date is not a serial."""
    if isinstance(serial, bool) or not isinstance(serial, (int, float)):
        return None
    days = float(serial)
    if not isinstance(fraction, bool) and isinstance(fraction, (int, float)):
        days += float(fraction)
    return SHEETS_EPOCH + timedelta(days=days)


class SpreadSheetService:

    # transaction_id lives in the last column (H); rows span A:H.
    ID_RANGE = 'data!H:H'
    APPEND_RANGE = 'data!A:H'
    RULES_RANGE = 'Sheet2!A2:B'
    ROWS_RANGE = 'data!A2:H'
    DATETIME_RANGE = 'data!A2:B'
    RULES_APPEND_RANGE = 'Sheet2!A:B'
    CATEGORY_COLUMN = 'D'
    # The Type column (income / expense / internal). See set_flow.
    FLOW_COLUMN = 'C'
    BUTTONS_RANGE = 'Категорії!A2:A'
    BUTTONS_APPEND_RANGE = 'Категорії!A:A'
    # Telegram loop state. A single cell holding one JSON object: the payload is
    # a handful of ids, orders of magnitude under Sheets' 50k-character limit.
    STATE_SHEET = '_state'
    STATE_RANGE = '_state!A1'
    # The digest's own cell. Separate from A1 on purpose: digest.py and main.py
    # are different dynos that can overlap, and sharing a cell would race on the
    # tagging loop's offset.
    DIGEST_STATE_RANGE = '_state!A2'

    def __init__(self, _auth_service, spreadsheet_id):
        # Keep the spreadsheets() handle, not just .values(): creating the
        # _state tab needs batchUpdate, which lives on the parent resource.
        self._spreadsheets = _auth_service.spreadsheets()
        self.sheet_service = self._spreadsheets.values()
        self.spreadsheet_id = spreadsheet_id

        # Get existing IDs (last column) and Sheet2 category-override rules
        self.existing_ids = self.get_existing_ids()
        self.rules = self.get_rules()

    def append_data(self, append_data: list) -> list:
        # transaction_id is the last element of each row
        unique_rows = [row for row in append_data if row[-1] not in self.existing_ids]

        # Guard clause against empty payloads causing HTTP 400
        if not unique_rows:
            logger.info("No unique rows to append. Skipping API call.")
            return []

        resource = {
            "majorDimension": "ROWS",
            "values": unique_rows
        }

        self.sheet_service.append(
            spreadsheetId=self.spreadsheet_id,
            range=self.APPEND_RANGE,
            body=resource,
            valueInputOption="USER_ENTERED"
        ).execute()

        logger.info(f'{len(unique_rows)} rows were successfully appended.')

        # Return exactly what was appended so main.py knows what to mark as read
        return [row[-1] for row in unique_rows]

    def get_existing_ids(self) -> set:
        # Fetch only the ID column to save bandwidth
        response = self.sheet_service.get(spreadsheetId=self.spreadsheet_id,
                                          range=self.ID_RANGE).execute()
        raw_values = response.get('values', [])

        if not raw_values or len(raw_values) <= 1:
            logger.warning(f'No values in sheet id {self.spreadsheet_id[:10]}')
            return set()

        # Extract ID from the column, skipping the header
        existing = {row[0] for row in raw_values[1:] if row}
        logger.info(f'Loaded {len(existing)} existing IDs from sheet id {self.spreadsheet_id[:10]}')
        return existing

    def get_rules(self) -> dict:
        # Sheet2 rules: column A = merchant/card to match, column B = category
        try:
            response = self.sheet_service.get(spreadsheetId=self.spreadsheet_id,
                                              range=self.RULES_RANGE).execute()
        except Exception as error:
            logger.warning(f'Could not read Sheet2 rules: {error}')
            return {}

        rules = {}
        for row in response.get('values', []):
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                rules[row[0].strip()] = row[1].strip()
        logger.info(f'Loaded {len(rules)} category-override rules from Sheet2')
        return rules

    def _already_ruled(self, merchant: str) -> bool:
        """Whether a Sheet2 rule already covers this counterparty.

        The rule IS the memory of "you have answered about this one". Reusing
        `apply_override` rather than re-implementing the match keeps the two
        from drifting: it understands the card last-4 form, exact text and
        substrings, and a rule that would fire at parse time is exactly the
        rule that should stop us asking again.
        """
        return apply_override(_NO_RULE, merchant, self.rules) is not _NO_RULE

    def _pending_kind(self, category: str, kind: str, amount: float, moment,
                      merchant: str = "") -> str:
        """Why this row needs the owner, or "" if it does not.

        Transfers are the second kind. The parser resolves them - the bank says
        it was a transfer - so the bot never asked, and that left 90 383 UAH of
        person-to-person transfers with no purpose recorded anywhere. They are
        queued now, but the owner cannot recall small ones from months back, so
        older rows are only asked about above TRANSFER_MIN_UAH.
        """
        if category == UNKNOWN:
            return "unknown"

        # Two more classes the parser resolves and therefore never asked about.
        # Both are answered once per counterparty rather than once per row: the
        # Sheet2 rule the answer writes is what silences the rest.
        if self._already_ruled(merchant):
            return ""

        # A payment by requisites names a person or a ФОП and records nothing
        # about what was bought. Queued at any size - there are about two a
        # month, and the owner asked for the historical ones too.
        if category == REQUISITES and kind == EXPENSE:
            return "requisites"

        # A transfer to one of the owner's own cards is only internal if the
        # money comes back. For a credit card it does not: *1111 shows 10 005
        # UAH out across three transfers and not one matching return, so the
        # repayment is the only record that money was ever spent. Direction
        # comes from the merchant text - the incoming leg is the other half of
        # the same movement, and asking about both is asking twice.
        if category == OWN_CARDS and kind == INTERNAL:
            if "зі своєї картки" in (merchant or "").lower():
                return ""
            return self._sized("own_card", amount, moment)

        # Movements: money crossed the account boundary but the row records no
        # purpose. Direction comes from the Type column, never from the
        # category - an incoming "Зарахування переказу" carries the same label
        # as an outgoing one, and the two need different questions.
        if category in TRANSFERS_OUT:
            movement = "transfer" if kind == EXPENSE else "incoming"
        elif category == CASH and kind == EXPENSE:
            movement = "cash"
        else:
            return ""

        return self._sized(movement, amount, moment)

    @staticmethod
    def _sized(movement: str, amount: float, moment) -> str:
        """The movement, or "" when it is too small and too old to recall.

        The owner cannot answer for a 5 UAH transfer from three weeks ago, and
        a guessed category is worse than a blank one.
        """
        if amount >= TRANSFER_MIN_UAH:
            return movement
        if moment and moment.date() >= TRANSFER_ASK_ALL_FROM:
            return movement
        return ""

    def pending_rows(self) -> list:
        """Rows the owner still needs to resolve: unresolved ones and transfers.

        Dates are read as serial numbers, not formatted strings: the rendered
        form follows the spreadsheet locale and the cutoff below compares real
        dates.
        """
        response = self.sheet_service.get(spreadsheetId=self.spreadsheet_id,
                                          range=self.ROWS_RANGE,
                                          valueRenderOption='UNFORMATTED_VALUE',
                                          dateTimeRenderOption='SERIAL_NUMBER').execute()
        pending = []
        for offset, row in enumerate(response.get('values', [])):
            if len(row) < 8:
                continue
            moment = _moment(row[0], row[1] if len(row) > 1 else None)
            raw_amount = row[5]
            amount = (float(raw_amount)
                      if isinstance(raw_amount, (int, float))
                      and not isinstance(raw_amount, bool) else 0.0)
            kind = self._pending_kind(str(row[3] or "").strip(),
                                      str(row[2] or "").strip(), amount, moment,
                                      str(row[4] or "").strip())
            if not kind:
                continue
            pending.append({
                # +2: the sheet is 1-based and row 1 is the header.
                "row": offset + 2,
                "kind": kind,
                "date": moment.strftime("%d.%m.%Y") if moment else "",
                "time": moment.strftime("%H:%M") if moment else "",
                "merchant": row[4],
                "amount": row[5],
                "tx_id": row[7],
            })
        return pending

    def set_category(self, row: int, category: str) -> None:
        # Column D only - never the id, never the amount.
        self.sheet_service.update(
            spreadsheetId=self.spreadsheet_id,
            range=f'data!{self.CATEGORY_COLUMN}{row}',
            body={"values": [[category]]},
            valueInputOption="USER_ENTERED",
        ).execute()

    def set_flow(self, row: int, kind: str) -> None:
        """Rewrite the Type column, and only it.

        Needed because `analytics.classify` groups a row by its Type, not its
        category: an own-card transfer the owner identifies as a credit
        repayment has to stop being `Переказ` or it stays dropped from every
        report no matter what category it carries.
        """
        self.sheet_service.update(
            spreadsheetId=self.spreadsheet_id,
            range=f'data!{self.FLOW_COLUMN}{row}',
            body={"values": [[kind]]},
            valueInputOption="USER_ENTERED",
        ).execute()

    def append_rule(self, merchant: str, category: str) -> None:
        key = (merchant or "").strip()
        if key in self.rules:
            # Already have this rule (either loaded at startup or written
            # earlier in this same run) - skip so one run can't write the
            # same rule twice.
            logger.info(f'Rule for {key!r} already exists; skipping duplicate write')
            return
        self.sheet_service.append(
            spreadsheetId=self.spreadsheet_id,
            range=self.RULES_APPEND_RANGE,
            body={"values": [[merchant, category]]},
            valueInputOption="USER_ENTERED",
        ).execute()
        self.rules[key] = category

    def category_frequencies(self) -> dict:
        # Drives keyboard ordering only - the owner's usual answers come first.
        response = self.sheet_service.get(spreadsheetId=self.spreadsheet_id,
                                          range=self.ROWS_RANGE,
                                          valueRenderOption='UNFORMATTED_VALUE',
                                          dateTimeRenderOption='FORMATTED_STRING').execute()
        counts = {}
        for row in response.get('values', []):
            if len(row) < 4 or not row[3] or row[3] == UNKNOWN:
                continue
            counts[row[3]] = counts.get(row[3], 0) + 1
        return counts

    def button_categories(self) -> list:
        """Category labels the bot may offer as buttons.

        Row order in this tab IS the button order — it is deliberate and is
        never re-sorted. Frequency ordering applies only to the built-in
        fallback list used when this tab is absent or empty.

        Deliberately separate from CANON: CANON maps raw bank text to a label
        automatically, while this list is only about what the keyboard shows.
        Removing a label here does not stop the parser recognising it.
        """
        try:
            response = self.sheet_service.get(spreadsheetId=self.spreadsheet_id,
                                              range=self.BUTTONS_RANGE).execute()
        except Exception as error:
            logger.warning(f'Could not read the Категорії tab: {error}')
            return []
        seen, labels = set(), []
        for row in response.get('values', []):
            label = row[0].strip() if row and row[0] else ''
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
        logger.info(f'Loaded {len(labels)} button categories')
        return labels

    def append_button_category(self, label: str) -> bool:
        """Add a category to the Категорії tab. True iff a row was written.

        Row order in that tab is the button order and is the owner's to curate,
        so a new label goes to the end and nothing is re-sorted.
        """
        clean = (label or "").strip()
        if not clean:
            return False
        existing = {item.strip().lower() for item in self.button_categories()}
        if clean.lower() in existing:
            logger.info(f'Category {clean!r} already offered; not appended')
            return False
        try:
            self.sheet_service.append(
                spreadsheetId=self.spreadsheet_id,
                range=self.BUTTONS_APPEND_RANGE,
                body={"values": [[clean]]},
                valueInputOption="USER_ENTERED",
            ).execute()
        except Exception as error:
            logger.warning(f'Could not append category {clean!r} '
                           f'({error.__class__.__name__})')
            return False
        logger.info(f'Added category {clean!r} to the Категорії tab')
        return True

    def latest_transaction_at(self):
        """Timestamp of the newest transaction in the sheet, or None.

        Read as serial numbers rather than formatted strings: the rendered form
        follows the spreadsheet's locale (it currently produces `M/D/YYYY`) and
        would misparse silently if that ever changed. Serials are unambiguous.

        Takes the maximum rather than the last row - rows are appended in the
        order mail arrives, and a backfill or an out-of-order notification means
        the bottom row is not reliably the newest.

        Returns None when the read fails or the sheet is empty; the caller
        treats "unknown" as "not stale" rather than crying wolf.
        """
        try:
            response = self.sheet_service.get(
                spreadsheetId=self.spreadsheet_id,
                range=self.DATETIME_RANGE,
                valueRenderOption='UNFORMATTED_VALUE',
                dateTimeRenderOption='SERIAL_NUMBER').execute()
        except Exception as error:
            logger.warning(f'Could not read transaction dates ({error.__class__.__name__})')
            return None

        newest = None
        for row in response.get('values', []):
            if not row or isinstance(row[0], bool) or not isinstance(row[0], (int, float)):
                continue
            serial = float(row[0])
            # Column B is a fraction of a day; absent or textual time just
            # leaves the timestamp at midnight, which is accurate enough for a
            # 48-hour threshold.
            if len(row) > 1 and not isinstance(row[1], bool) and isinstance(row[1], (int, float)):
                serial += float(row[1])
            if newest is None or serial > newest:
                newest = serial

        if newest is None:
            return None
        return SHEETS_EPOCH + timedelta(days=newest)

    def all_rows(self) -> list[dict]:
        """Every transaction row as a plain dict, for the digest.

        Serial numbers rather than formatted strings, for the same reason as
        latest_transaction_at: the rendered date follows the spreadsheet locale
        and would misparse silently if that ever changed.

        Rows that cannot be trusted are dropped rather than guessed at - a text
        date or a text amount means something wrote into the sheet by hand, and
        a digest that silently invents a value is worse than one row short.
        """
        response = self.sheet_service.get(
            spreadsheetId=self.spreadsheet_id,
            range=self.ROWS_RANGE,
            valueRenderOption='UNFORMATTED_VALUE',
            dateTimeRenderOption='SERIAL_NUMBER').execute()

        rows = []
        for raw in response.get('values', []):
            if len(raw) < 8:
                continue
            serial, amount = raw[0], raw[5]
            # bool is a subclass of int in Python; TRUE in a cell is not a date.
            if isinstance(serial, bool) or not isinstance(serial, (int, float)):
                continue
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                continue
            days = float(serial)
            if not isinstance(raw[1], bool) and isinstance(raw[1], (int, float)):
                days += float(raw[1])
            # Balance is carried but never used in arithmetic: with several
            # cards the column jumps between them. The digest only ever quotes
            # the newest one, labelled with the transaction it followed.
            balance = raw[6] if len(raw) > 6 else None
            usable = (isinstance(balance, (int, float))
                      and not isinstance(balance, bool))
            rows.append({
                "date": SHEETS_EPOCH + timedelta(days=days),
                "kind": str(raw[2] or "").strip(),
                "category": str(raw[3] or "").strip(),
                "merchant": str(raw[4] or "").strip(),
                "amount": float(amount),
                "balance": float(balance) if usable else None,
            })
        logger.info(f'Loaded {len(rows)} rows for the digest')
        return rows

    def read_state(self, cell: str | None = None) -> str | None:
        """The stored JSON for a state cell, or None if there is none yet.

        Never raises: a missing tab is the ordinary first-run case, and the
        caller degrades to defaults. Only the exception class is logged - the
        detail carries the spreadsheet id and says nothing useful here.
        """
        try:
            response = self.sheet_service.get(
                spreadsheetId=self.spreadsheet_id,
                range=cell or self.STATE_RANGE,
                valueRenderOption='UNFORMATTED_VALUE').execute()
        except Exception as error:
            logger.info(f'No state in the sheet ({error.__class__.__name__})')
            return None

        values = response.get('values', [])
        if not values or not values[0]:
            return None
        text = values[0][0]
        # A non-string cell (someone typed a number into A1) is not our state.
        return text if isinstance(text, str) and text.strip() else None

    def write_state(self, text: str, cell: str | None = None) -> None:
        """Store a state cell's JSON, creating the tab if it is missing.

        The retry matters: if this write never lands, `asked` is never
        persisted, the 12-hour cooldown cannot apply, and every pending row is
        asked about again on every single run.
        """
        try:
            self._write_state_cell(text, cell)
        except HttpError:
            logger.info(f'State write failed; creating the {self.STATE_SHEET} tab and retrying')
            self._create_state_sheet()
            self._write_state_cell(text, cell)

    def _write_state_cell(self, text: str, cell: str | None = None) -> None:
        # RAW, not USER_ENTERED: the payload opens with "{" and must be stored
        # verbatim rather than interpreted by Sheets.
        self.sheet_service.update(
            spreadsheetId=self.spreadsheet_id,
            range=cell or self.STATE_RANGE,
            body={"values": [[text]]},
            valueInputOption="RAW",
        ).execute()

    def _create_state_sheet(self) -> None:
        try:
            self._spreadsheets.batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {
                    "title": self.STATE_SHEET,
                    "hidden": True,
                }}}]},
            ).execute()
            logger.info(f'Created the hidden {self.STATE_SHEET} tab')
        except HttpError as error:
            # "Already exists" is fine - a concurrent run won the race. Any
            # other cause resurfaces on the retry the caller is about to make.
            logger.info(f'Could not create the {self.STATE_SHEET} tab '
                        f'({error.__class__.__name__}); it may already exist')


if __name__ == '__main__':
    auth = AuthService()
    sheet_service = SpreadSheetService(auth.service_sheets, auth.spreadsheet_id)
    print(sheet_service.existing_ids)


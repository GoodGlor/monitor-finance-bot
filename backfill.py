"""One-off: re-parse archived emails and report what would change.

Dry run by default. Pass --apply to write (Task 8).
"""
import logging
import sys

from auth_service import AuthService
from categories import apply_override, UNKNOWN
from gmail_service import GmailService
from spread_sheet_service import SpreadSheetService

logger = logging.getLogger(__name__)

# Sheet columns, 0-based, as read from A:H.
TYPE, CATEGORY, MERCHANT, AMOUNT, TX_ID = 2, 3, 4, 5, 7
FIELD_NAMES = {TYPE: "Type", CATEGORY: "Category", MERCHANT: "Merchant", AMOUNT: "Amount"}


def load_sheet_rows(sheets):
    # UNFORMATTED_VALUE: formatted values would come back locale-formatted
    # ("5 661,69") and comparing or rewriting those would corrupt the numbers.
    resp = sheets.sheet_service.get(
        spreadsheetId=sheets.spreadsheet_id,
        range='data!A2:H',
        valueRenderOption='UNFORMATTED_VALUE',
        dateTimeRenderOption='FORMATTED_STRING',
    ).execute()
    return resp.get('values', [])


def recompute(gmail, sheets, tx_id):
    snippet = gmail.gmail_service.get(userId='me', id=tx_id).execute().get('snippet', '')
    parsed = GmailService.extract_transaction_data(snippet)
    if not parsed:
        return None
    parsed["category"] = apply_override(parsed["category"], parsed["merchant"], sheets.rules)
    return parsed


def main(apply=False):
    logging.basicConfig(level=logging.INFO)
    auth = AuthService()
    gmail = GmailService(auth.service_gmail)
    sheets = SpreadSheetService(auth.service_sheets, auth.spreadsheet_id)
    gmail.rules = sheets.rules

    rows = load_sheet_rows(sheets)
    changes = []
    protected = 0
    for i, row in enumerate(rows, start=2):
        if len(row) < 8 or not str(row[TX_ID]).strip():
            logger.warning(f'row {i}: short row or missing id, skipped')
            continue
        new = recompute(gmail, sheets, row[TX_ID])
        if not new:
            logger.warning(f'row {i}: email {row[TX_ID]} no longer parses, left untouched')
            continue
        proposed = [new["is_income"], new["category"], new["merchant"], new["amount"]]
        current = [row[TYPE], row[CATEGORY], row[MERCHANT], row[AMOUNT]]

        # A human's Telegram answer must never be recomputed away: an
        # unlearnable merchant (e.g. "На картку") writes no Sheet2 rule, so a
        # fresh recompute yields UNKNOWN again even though the owner already
        # resolved it. Applying that would erase the answer and re-queue the
        # row. Drop the WHOLE row - its other proposed fields came from the
        # same recompute pass and a partial write would be worse.
        current_category = str(row[CATEGORY]).strip()
        if proposed[1] == UNKNOWN and current_category and current_category != UNKNOWN:
            protected += 1
            logger.info(f'row {i}: keeping human answer {current_category!r}; '
                       f'recompute would downgrade it to {UNKNOWN!r}, skipped')
            continue

        def differs(col, old, new_val):
            if col == AMOUNT:
                try:
                    return abs(float(old) - float(new_val)) > 0.005
                except (TypeError, ValueError):
                    return str(old) != str(new_val)
            return str(old) != str(new_val)

        diffs = [(FIELD_NAMES[c], old, propose)
                 for c, old, propose in zip((TYPE, CATEGORY, MERCHANT, AMOUNT), current, proposed)
                 if differs(c, old, propose)]
        if diffs:
            changes.append((i, row[TX_ID], diffs, proposed))

    with open('backfill_diff.txt', 'w') as fh:
        for i, tid, diffs, _ in changes:
            fh.write(f'row {i}  {tid}\n')
            for field, old, new_val in diffs:
                fh.write(f'    {field}: {old!r} -> {new_val!r}\n')
        fh.write(f'\n{len(changes)} of {len(rows)} rows would change\n')
        fh.write(f'{protected} human answer(s) protected from downgrade to {UNKNOWN!r}\n')
    print(f'{len(changes)} of {len(rows)} rows would change -> backfill_diff.txt')
    print(f'{protected} human answer(s) protected from downgrade to {UNKNOWN!r}')

    if not apply:
        return changes, sheets

    # C:F only - Date, Time, Balance and transaction_id are never rewritten,
    # so the dedup ledger and the date values cannot drift.
    data = [{"range": f'data!C{i}:F{i}', "values": [proposed]}
            for i, _tid, _diffs, proposed in changes]
    for start in range(0, len(data), 100):
        chunk = data[start:start + 100]
        sheets.sheet_service.batchUpdate(
            spreadsheetId=sheets.spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": chunk},
        ).execute()
        logger.info(f'applied rows {start + 1}..{start + len(chunk)}')
    print(f'applied {len(changes)} rows')
    return changes, sheets


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)

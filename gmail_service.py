from datetime import datetime
from auth_service import AuthService
from categories import (
    OTHER_INCOME, UNKNOWN, apply_override, categorize, flow_after_override,
    is_own_card_transfer,
)
import html
import re
import logging

logger = logging.getLogger(__name__)


class GmailService:
    def __init__(self, _auth_service):
        self.gmail_service = _auth_service.users().messages()
        # Set by main.py from SpreadSheetService before processing.
        self.rules = {}

    @staticmethod
    def extract_transaction_data(text: str) -> dict | None:
        # Regex designed to capture all possible PrivatBank formats
        # Handles optional signs, dual currencies ($ and UAH conversions), optional commissions
        pattern = r"^(?P<sign>[+\-])?\s*(?P<amount>\d[\d\s]*(\.\d+)?)\s*(?P<currency>[$₴UAH]+)(?:\((?P<amount_uah>\d[\d\s]*(\.\d+)?)\s*(?:₴|UAH)\))?\s+(?P<raw_desc>.*?)\s+(?P<card>\d\*\d{2})\s+(?P<time>\d{2}:\d{2})(?:\s+Комісія\s+(?P<commission>\d[\d\s]*(\.\d+)?)\s*(?:₴|UAH))?\s+Бал\.\s*(?P<balance>[\d\s]+(?:\.\d+)?)\s*(?:₴|UAH)"

        match = re.search(pattern, text)
        if not match:
            return None

        data = match.groupdict()
        raw_desc = data["raw_desc"].strip()

        # "Повернення. Таксі. Bolt" (or "Повернення, Таксі Bolt") is a refund
        # OF Таксі, not its own category.
        is_refund = raw_desc.lower().startswith("повернення")
        if is_refund:
            parts = re.split(r"[.,]", raw_desc, maxsplit=1)
            if len(parts) > 1:
                raw_desc = parts[1].strip()

        # Bank boilerplate for double-conversion purchases is not part of the name.
        raw_desc = raw_desc.replace("З подвійною конвертацією pb.ua/conv", "").strip()

        sign = data.get("sign")
        desc_low = raw_desc.lower()

        # own_transfer only says whether this leg is an own-card move (for the
        # "Переказ" type override below); it does not decide direction.
        own_transfer = is_own_card_transfer(raw_desc)

        # An explicit +/- sign is authoritative when present - the wording
        # rules below exist only to cover the cases where the bank omits it.
        if sign == "+":
            direction = "in"
        elif sign == "-":
            direction = "out"
        elif is_refund:
            direction = "in"
        elif "переказ на свою картку" in desc_low:
            direction = "out"
        elif "переказ зі своєї картки" in desc_low:
            direction = "in"
        else:
            # "повернення" is not listed here: refunds are handled by is_refund above.
            income_triggers = ["зарплата", "зарахування", "стипендія"]
            direction = "in" if any(t in desc_low for t in income_triggers) else "out"

        if own_transfer:
            is_income = "Переказ"
        else:
            is_income = "Дохід" if direction == "in" else "Витрата"

        raw_amount = float(data["amount"].replace(" ", ""))
        if data.get("amount_uah"):
            final_amount = float(data["amount_uah"].replace(" ", ""))
        else:
            final_amount = raw_amount

        balance = float(data["balance"].replace(" ", ""))
        commission = float(data["commission"].replace(" ", "")) if data.get("commission") else 0.0

        # Debit cards report an amount that already includes the fee. The credit
        # card charges it on top - detectable by the credit-limit line the bank
        # prints only for that card. Incoming transfers are credited net of fee.
        # Commission is always quoted in UAH, so it can only be folded into an
        # amount that is itself in UAH - either the amount_uah equivalent or a
        # native-UAH figure - never into a raw $ figure.
        if commission:
            currency = data.get("currency", "")
            fee_is_uah_denominated = bool(data.get("amount_uah")) or "$" not in currency
            if not fee_is_uah_denominated:
                logger.warning(
                    f"Commission {commission} UAH present but amount currency is "
                    f"{currency!r}; fee not applied to avoid mixing currencies."
                )
            elif direction == "in":
                final_amount -= commission
            elif "Кред. ліміт" in text:
                final_amount += commission
            else:
                # The credit-limit clause sits at the very end of the snippet and
                # Gmail truncates long snippets, so its absence can mean either
                # "debit card" or "truncated". Either way the fee is assumed
                # already included, but make that assumption visible.
                logger.info(
                    "Commission present with no 'Кред. ліміт' clause; fee "
                    "assumed already included in the amount."
                )
        final_amount = round(final_amount, 2)

        # Parse the semi-structured description block into category and merchant

        category = ""
        merchant = ""

        if raw_desc.startswith("Оплата частинами"):
            category = "Оплата частинами"
            merchant = raw_desc.replace("Оплата частинами", "").strip()
        elif "." in raw_desc:
            # Split by the first dot to separate category and merchant
            parts = raw_desc.split(".", 1)
            category = parts[0].strip()
            merchant = parts[1].strip()
        elif "," in raw_desc:
            # Fallback for formats like "Зарплата, ТОВ ..."
            parts = raw_desc.split(",", 1)
            category = parts[0].strip()
            merchant = parts[1].strip()
        else:
            # Failsafe if no clear delimiter exists
            category = raw_desc
            merchant = raw_desc

        # Застосовуємо смарт-категоризацію
        final_category = categorize(category, merchant)

        # Direction is known reliably, so an unidentifiable *incoming* transaction
        # does not need a human: money arrived, and that is the whole story.
        # Outgoing unknowns still queue, because where the money went matters.
        if final_category == UNKNOWN and direction == "in":
            final_category = OTHER_INCOME

        return {
            "is_income": is_income,
            "direction": direction,
            "category": final_category,
            "merchant": html.unescape(merchant),
            "card": data["card"],
            "commission": commission,
            "amount": final_amount,
            "balance": balance,
        }

    def request_mails(self, q='from:info@pb.ua'):
        messages = []
        page_token = None
        while True:
            response = self.gmail_service.list(
                userId='me',
                labelIds=['UNREAD', 'INBOX'],
                includeSpamTrash=False,
                q=q,
                pageToken=page_token,
            ).execute()
            messages.extend(response.get('messages', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        logger.info(f'Requested mails was successful ({len(messages)} found)')

        return messages

    def mark_as_read(self, ids_mails: list):

        # Must contain logic to remove UNREAD label via Gmail API
        if not ids_mails:
            logger.warning('No mails to read')
            return
        self.gmail_service.batchModify(userId='me',
                                       body={
                                           "ids": ids_mails,
                                           "removeLabelIds": ["UNREAD"]}).execute()

        logger.info(f'Marked as read: {ids_mails}')

    def format_snippet(self, id_mail: str, text_mail: str) -> dict | None:
        # Delegate parsing to the regex function. No hardcoded email checks.
        transaction_data = self.extract_transaction_data(text_mail)

        if not transaction_data:
            # If regex fails, it is not a valid transaction email. Mark as read.
            logger.warning(f"Skipped non-transactional email: {id_mail}")
            return None

        return transaction_data

    def get_mail(self, mail_id: str) -> dict | None:
        response = self.gmail_service.get(userId='me', id=mail_id).execute()

        id_mail = response.get('id', '')
        snippet = response.get('snippet', '')

        # Process the snippet using the regex logic
        parsed_result = self.format_snippet(id_mail, snippet)

        if parsed_result:
            # Extract and format the date only if parsing was successful
            internal_date = int(response.get('internalDate', '0')) / 1000
            from_timestamp = datetime.fromtimestamp(internal_date)

            # Inject metadata into the final result. Date is ISO so Sheets stores
            # it as a real date; display formatting is applied on the sheet.
            parsed_result["id"] = id_mail
            parsed_result["date"] = from_timestamp.strftime("%Y-%m-%d")
            parsed_result["time"] = from_timestamp.strftime("%H:%M")

            # Sheet2 override rules (merchant/card -> forced category)
            parsed_result["category"] = apply_override(
                parsed_result["category"], parsed_result["merchant"], self.rules)
            # A rule can also settle whether the row is internal at all: once
            # the owner has said *1111 is a credit card, a transfer to it is
            # spending, and only the Type column makes it visible to reports.
            parsed_result["is_income"] = flow_after_override(
                parsed_result["is_income"], parsed_result["category"],
                parsed_result["merchant"])
            logger.info(f'Mail {mail_id} was parsed')
            return parsed_result
        logger.warning(f'Mail {mail_id} was not parsed !')
        return None

    @staticmethod
    def sort_rows(rows: list) -> list:
        # Gmail lists newest first; the sheet must read oldest first. Date and
        # time are written as zero-padded ISO strings, so string order is
        # chronological order. This orders each batch - it cannot reorder rows
        # already committed to the sheet.
        return sorted(rows, key=lambda r: (r[0], r[1]))

    @staticmethod
    def format_for_sheets(data_dict: dict) -> list:
        # Column order: A..H -> Date, Time, Type, Category, Merchant, Amount,
        # Balance, transaction_id
        return [
            data_dict.get("date", ""),
            data_dict.get("time", ""),
            data_dict.get("is_income", ""),
            data_dict.get("category", ""),
            data_dict.get("merchant", ""),
            data_dict.get("amount", 0.0),
            data_dict.get("balance", 0.0),
            data_dict.get("id", ""),
        ]

    def process_all_unread(self, existing_ids: set) -> dict:
        messages = self.request_mails()
        if not messages:
            logger.warning("No new messages found.")
            # FIX: Maintain strict return type schema
            return {"sheets_rows": [], "valid_ids": []}

        invalid_mail_ids = []
        already_processed_ids = []
        valid_ids = []
        sheets_rows = []
        for message in messages:
            message_id = message.get('id')

            # PRE-CHECK: If mail is already in sheets, skip parsing and mark as read
            if message_id in existing_ids:
                logger.info(f"Mail {message_id} is already in the sheet. Skipping parse.")
                already_processed_ids.append(message_id)
                continue

            mail = self.get_mail(message_id)
            if mail:
                sheets_rows.append(self.format_for_sheets(mail))
                valid_ids.append(message_id)
            else:
                invalid_mail_ids.append(message_id)

        # Mark both invalid and already processed emails as read
        ids_to_mark_read = invalid_mail_ids + already_processed_ids
        if ids_to_mark_read:
            self.mark_as_read(ids_to_mark_read)

        logger.info(
            f"Mails sorted. {len(invalid_mail_ids)} invalid and {len(already_processed_ids)} already processed mails were marked as read.")

        return {"sheets_rows": self.sort_rows(sheets_rows), "valid_ids": valid_ids}


if __name__ == '__main__':
    auth = AuthService()
    gmail_service = GmailService(auth.service_gmail)
    print(gmail_service.get_mail('19c95317b7f1f7a9'))
    # gmail_service.process_all_unread()

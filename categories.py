"""Shared transaction-category logic.

Used by both the live pipeline (gmail_service) and one-off migrations, so the
categories produced for new emails match the ones written to historical rows.
"""
import html
import re

UNKNOWN = "Не визначено"

# Money arrived but nothing identifies the source. The owner cannot resolve this
# any better than the parser can, so it is labelled rather than queued.
OTHER_INCOME = "Інші надходження"

# A leg the bank describes as moving money between the owner's own cards. Named
# because it is load-bearing in three places now: the transfer subcategory, the
# tagging queue, and the flow classifier. It is NOT reliably internal - a card
# in this set can be a credit card, where the money leaves and never returns.
OWN_CARDS = "Перекази — свої картки"

# The bank names the category and nothing else: the merchant column holds a
# person or a ФОП and the row records no purpose at all.
REQUISITES = "Платежі за реквізитами"

# Canonical labels keyed by the lowercased category text PrivatBank puts in the
# email (also matches labels produced by earlier versions of this code).
CANON = {
    "продукти": "Продукти",
    "супермаркети та продукти": "Продукти",
    "транспорт": "Транспорт",
    "таксі": "Таксі",
    "ресторани, кафе, бари": "Кафе та ресторани",
    "кафе та ресторани": "Кафе та ресторани",
    "дім та ремонт": "Дім та ремонт",
    "послуги": "Послуги",
    "розваги": "Розваги",
    "азс": "Автомобіль",
    "авто": "Автомобіль",
    "авто / пальне": "Автомобіль",
    "зарплата": "Зарплата",
    "стипендія": "Стипендія",
    "аптеки": "Аптеки",
    "аптеки та медицина": "Аптеки",
    "цифрові товари": "Цифрові товари",
    "платежі за реквізитами": REQUISITES,
    "платежі до бюджету": "Платежі до бюджету",
    "повернення": "Повернення",
    "тварини": "Тварини",
    "підписки / зв'язок": "Підписки / Зв'язок",
    "квіти": "Квіти",
    "оплата частинами": "Оплата частинами",
    "книги та канцтовари": "Книги та канцтовари",
    "покупки / побут": "Покупки / Побут",
    "комуналка": "Комуналка",
    "інтернет": "Інтернет",
    "зняття готівки": "Зняття готівки",
    # Already a live label on six rows via the "кредит готівкою" prefix rule
    # below, but absent from this table - so a free-text answer of "Кредит"
    # found no local match, went to a model, and came back "Переказ рідним".
    "кредит": "Кредит",
    "погашення кредиту": "Кредит",
    "кредит готівкою": "Кредит",
    "ресторани": "Кафе та ресторани",
    "краса": "Краса",
    "освіта": "Освіта",
    "страхування": "Страхування",
    "інші надходження": "Інші надходження",
}

# Cash left the account but was not consumed - it moved to a pocket.
CASH = "Зняття готівки"

# Money left the account but went to someone else rather than being consumed.
# Shared so the digest and the tagging queue agree on what counts.
TRANSFERS_OUT = ("Перекази — людям / P2P", "Перекази — сервіси")

# Raw categories that denote a transfer; the subcategory is decided from text.
TRANSFER_CATS = {
    "перекази", "переказ", "переказ на свою картку",
}

# Keyword fallback (canonical -> keywords), matched on word boundaries so short
# tokens like "сто" (СТО) don't match inside words like "місто".
KEYWORDS = {
    "Продукти": ["сільпо", "silpo", "атб", "atb", "novus", "ашан", "auchan",
                 "фора", "fora", "metro", "велмарт", "маркет"],
    "Кафе та ресторани": ["mcdonalds", "kfc", "aroma", "кафе", "ресторан",
                          "ресторани", "кав'ярня", "glovo", "bolt food", "пузата"],
    "Транспорт": ["метро", "metropoliten", "kyivpastrans", "укрзалізниця",
                  "uz.gov.ua"],
    "Таксі": ["uber", "uklon", "таксі"],
    "Автомобіль": ["wog", "okko", "upg", "socar", "brsm", "авіас", "shell",
                   "заправка", "пальне"],
    "Аптеки": ["аптека", "аптеки", "anc", "подорожник", "синево", "synevo",
               "діла", "dila"],
    "Розваги": ["кіно", "multiplex", "боулінг", "concert"],
    "Підписки / Зв'язок": ["netflix", "youtube", "spotify", "megogo",
                           "kyivstar", "vodafone", "lifecell"],
    "Комуналка": ["ясно", "yasno", "нафтогаз", "водоканал", "опалення"],
    "Інтернет": ["internet", "інтернет"],
}


# The three values the Type column takes. Defined here, with the categories
# they have to agree with, because three modules decide things from them:
# analytics groups by Type, the tagging queue reads it, and the parser writes
# it. Three private copies of the same string is how they drift apart.
INCOME = "Дохід"
EXPENSE = "Витрата"
INTERNAL = "Переказ"


def flow_after_override(kind: str, category: str, merchant: str) -> str:
    """The Type a row carries once its category is final.

    An own-card transfer is internal only for as long as its category still
    says so. A Sheet2 rule that renames it - `*1111` -> `Кредит` - is the owner
    stating that this particular card is a credit card, where the money leaves
    and never returns and the repayment is the only record of the spending.
    `analytics.classify` drops a row by Type alone, so leaving it `Переказ`
    would keep it out of every report no matter what category it now carries.

    The incoming leg is deliberately left alone. It reads the same in the Type
    column, and money arriving is not spending; flipping it would book income
    as an expense.
    """
    if kind != INTERNAL or category == OWN_CARDS:
        return kind
    if "зі своєї картки" in (merchant or "").lower():
        return kind
    return EXPENSE


def is_own_card_transfer(text: str) -> bool:
    """True when the bank states this moved money between the owner's own cards."""
    low = html.unescape((text or "")).lower()
    return "на свою картку" in low or "зі своєї картки" in low


def _is_transfer(low: str) -> bool:
    return (low in TRANSFER_CATS
            or low.startswith("зарахування переказу")
            or low.startswith("переказ зі своєї картки")
            or is_own_card_transfer(low))


def _transfer_subcat(cat_low: str, merch_low: str) -> str:
    if ("своєї картки" in cat_low or "на свою картку" in cat_low
            or "своєї картки" in merch_low or "на свою картку" in merch_low):
        return OWN_CARDS
    if "novapay" in merch_low or "p2p" in merch_low or "piraeus" in merch_low:
        return "Перекази — сервіси"
    return "Перекази — людям / P2P"


def _keyword_hit(cat_low: str, merch_low: str):
    for canon, kws in KEYWORDS.items():
        for kw in kws:
            pat = r'\b' + re.escape(kw) + r'\b'
            if re.search(pat, merch_low) or re.search(pat, cat_low):
                return canon
    return None


def categorize(raw_cat: str, raw_merch: str) -> str:
    """Map a raw PrivatBank category + merchant to one canonical category."""
    cat = html.unescape((raw_cat or "").strip())
    merch = html.unescape((raw_merch or "").strip())
    low, ml = cat.lower(), merch.lower()

    if _is_transfer(low):
        return _transfer_subcat(low, ml)
    if low in CANON:
        return CANON[low]
    if low == "комуналка та інтернет":
        # PrivatBank lumps both under one category; split by merchant text.
        if re.search(r'\binternet\b', ml) or "інтернет" in ml or "portmone" in ml:
            return "Інтернет"
        return "Комуналка"
    if low.startswith("зняття готівки"):
        return "Зняття готівки"
    if low.startswith("кредит готівкою"):
        return "Кредит"
    hit = _keyword_hit(low, ml)
    if hit:
        return hit
    return UNKNOWN


def apply_override(category: str, merchant: str, rules: dict) -> str:
    """Force a category when the merchant matches a Sheet2 rule.

    A rule key (`rules` maps match-text -> category) can be:
      * a full card number, e.g. "4441111100003333" — matched against the
        "*NNNN" last-4 form PrivatBank writes into transfer text ("*3333");
      * the last-4 form itself, e.g. "*3333";
      * a person/merchant substring, e.g. "Ткачук";
      * exact merchant text.
    Exact match wins, then card-number (last-4) match, then substring.
    """
    if not rules:
        return category
    m = (merchant or "").strip()
    ml = m.lower()
    for key, new_cat in rules.items():
        k = (key or "").strip()
        if not k:
            continue
        if m == k:
            return new_cat
        digits = ''.join(ch for ch in k if ch.isdigit())
        if len(digits) >= 4 and ("*" + digits[-4:]) in m:
            return new_cat
        if k.lower() in ml:
            return new_cat
    return category


# The bank writes the card's last four as "*NNNN" into transfer text, and
# apply_override depends on that form. It must not travel to a third party.
CARD_TAIL = re.compile(r"\*\s*\d{4}")

# Contract and account numbers: "Кредит готівкою 12345678901234". Five digits
# is the floor so a brand like "WOG 24" survives.
LONG_NUMBER = re.compile(r"\d{5,}")

# The Ukrainian ПІБ pattern - three capitalised Cyrillic words. Live data put
# "Лисенко Олег Петрович" into Платежі за реквізитами, which is a spend
# category, so the name reached top_merchants with nothing to stop it.
FULL_NAME = re.compile(
    r"\b[А-ЯІЇЄҐ][а-яіїєґ']+\s+[А-ЯІЇЄҐ][а-яіїєґ']+\s+[А-ЯІЇЄҐ][а-яіїєґ']+\b")

# "Петренко А.", "Іваненко І.П." - how the bank writes a transfer counterparty.
# Two tokens, so FULL_NAME misses it. Tagging a transfer turns it into ordinary
# spend, at which point the name would reach top_merchants.
NAME_INITIALS = re.compile(
    r"\b[А-ЯІЇЄҐ][а-яіїєґ']+\s+[А-ЯІЇЄҐ]\.\s*(?:[А-ЯІЇЄҐ]\.)?")


def scrub_merchant(name: str) -> str:
    """A merchant name safe to send to a third party.

    Strips the card tail, contract numbers and personal names. The owner
    approved sending merchants meaning venues and services; a counterparty's
    full name is neither, and OpenRouter's free tier logs every prompt.

    Lives here rather than in digest.py because openrouter_client needs it too,
    and digest imports openrouter_client - the other direction would be a cycle.
    """
    cleaned = CARD_TAIL.sub("", name or "")
    cleaned = LONG_NUMBER.sub("", cleaned)
    cleaned = FULL_NAME.sub("особа", cleaned)
    cleaned = NAME_INITIALS.sub("особа", cleaned)
    return " ".join(cleaned.split())


def match_text(text: str, choices: list) -> str:
    """Resolve free text to a category label locally, or "" if it cannot.

    Deliberately reuses categorize() rather than growing a second vocabulary:
    two keyword tables would drift, and the digest's baselines are computed
    against whatever the first one produced.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    for label in choices or ():
        if label.lower() == lowered:
            return label
    resolved = categorize(cleaned, cleaned)
    if resolved != UNKNOWN:
        return resolved

    # CANON is keyed by the bank's exact category text, so categorize() only
    # ever looks it up whole. Free text is a sentence, and the commonest answer
    # there is - "продукти на тиждень" - has the key sitting inside it.
    # Longest first: "супермаркети та продукти" must not lose to "продукти".
    for key in sorted(CANON, key=len, reverse=True):
        if re.search(r'\b' + re.escape(key) + r'\b', lowered):
            return CANON[key]
    return ""

"""Transport to OpenRouter, plus deciding which free model to ask.

Never raises and never logs the API key: the digest must degrade to numbers
without an analysis rather than fail. The key guard follows the precedent set
for the Telegram token, where http.client.InvalidURL embedded the request path
- and therefore the credential - in its message.
"""
import json
import logging
import re
import time
import urllib.request

from categories import UNKNOWN, scrub_merchant

logger = logging.getLogger(__name__)

MODELS_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

TIMEOUT = 30

# Tried first when present in the live list. Verified free and text-to-text on
# 2026-08-07. The list rots, which is why it is only a preference: the live
# catalogue decides what actually exists.
PREFERRED = (
    # Instruction-tuned first. nemotron-3-ultra is a reasoning model and
    # answered with its English chain-of-thought instead of the Ukrainian text
    # it was asked for, so it is a fallback rather than the default.
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-20b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)

# OpenRouter's own auto-router over free models. Last, because it gives no
# control over which model answers.
LAST_RESORT = "openrouter/free"

# Free, text-to-text, and useless here: classifiers and embedding endpoints
# answer nothing a reader would want.
DENY = ("content-safety", "guard", "moderation", "embed", "rerank")


def _get(url: str, timeout: int) -> bytes:
    """The single read primitive, isolated so tests can replace it."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _is_free(entry: dict) -> bool:
    pricing = entry.get("pricing") or {}
    zero = ("0", "0.0", 0, 0.0)
    return pricing.get("prompt") in zero and pricing.get("completion") in zero


def _is_text(entry: dict) -> bool:
    """Text in, text and only text out.

    Not optional: a live check found google/lyria-3-pro-preview - a music
    generator - among the free models, and it passes every other filter.
    """
    arch = entry.get("architecture") or {}
    return ("text" in (arch.get("input_modalities") or [])
            and list(arch.get("output_modalities") or []) == ["text"])


def free_models(timeout: int = TIMEOUT) -> list[str]:
    """Ranked candidate ids: preferences first, then widest context, router last.

    The catalogue needs no authorization, so this still works when the key has
    expired. If it cannot be read at all, fall back to the hard-coded
    preferences and try blind - that list rots, but it beats sending nothing.
    """
    fallback = list(PREFERRED) + [LAST_RESORT]
    try:
        entries = json.loads(_get(MODELS_URL, timeout).decode())["data"]
    except Exception as error:
        logger.warning(f'Could not read the model list ({error.__class__.__name__}); '
                       f'falling back to the preferred list')
        return fallback

    usable = []
    for entry in entries:
        mid = entry.get("id") or ""
        if not mid or not _is_free(entry) or not _is_text(entry):
            continue
        if any(bad in mid for bad in DENY):
            continue
        usable.append((mid, entry.get("context_length") or 0))

    available = {mid for mid, _ in usable}
    ranked = [mid for mid in PREFERRED if mid in available]
    rest = sorted((pair for pair in usable
                   if pair[0] not in ranked and pair[0] != LAST_RESORT),
                  key=lambda pair: pair[1], reverse=True)
    ranked += [mid for mid, _ in rest]
    if LAST_RESORT in available:
        ranked.append(LAST_RESORT)
    return ranked or fallback


# A rate-limited model answers in milliseconds, so a low attempt count threw
# away every remaining candidate for nothing: two 429s used to exhaust the
# allowance while eleven untried models were still listed. The wall-clock
# budget is the real bound; this is only a runaway guard.
MAX_ATTEMPTS = 8
MAX_TOKENS = 1200

# Wall-clock ceiling for the whole analysis, across every attempt. urlopen's
# timeout is per-read, not total: a model that streams slowly holds the
# connection far longer than TIMEOUT, so counting attempts was never a real
# bound. The daily job can afford two minutes; the /week command runs inside a
# loop that fires every ten, and must not stretch into the next one.
TOTAL_BUDGET = 180
MAX_ANALYSIS_CHARS = 1500

# A number this small in prose is a count or a percentage, not a claim about
# money, so it is not worth checking against the input.
NUMBER_CHECK_FLOOR = 100

SYSTEM_PROMPT = (
    "Ти фінансовий аналітик. Тобі дають ГОТОВІ агрегати за період у форматі JSON. "
    "Не рахуй нічого сам і не називай жодних чисел, яких немає у вхідних даних. "
    "НЕ ПЕРЕКАЗУЙ цифри — читач бачить їх у звіті над твоїм текстом. "
    "Пояснюй, що вони означають, чому так вийшло і що з цього випливає. "
    "Називай конкретних мерчантів, коли вони пояснюють категорію. "
    "Відповідай українською, звичайним текстом без markdown, максимум 120 слів. "
    "Формат: один-два короткі абзаци, потім рядок «Що варто зробити:» "
    "і рівно три конкретні дії, кожна з нового рядка з «• ». "
    "Кожен рядок завершуй крапкою."
)


def _post(url: str, body: bytes, headers: dict, timeout: int) -> bytes:
    """The single write primitive, isolated so tests can replace it."""
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _redact(text: str, api_key: str) -> str:
    """Some exceptions embed the request, and therefore the credential."""
    if not api_key:
        return text
    cleaned = text.replace(api_key, "***")
    escaped = repr(api_key)[1:-1]
    if escaped != api_key:
        cleaned = cleaned.replace(escaped, "***")
    return cleaned


def _is_the_answer(text: str) -> bool:
    """Whether this looks like the Ukrainian prose we asked for.

    A reasoning model can return its own scratchpad in `content` - the first
    live attempt came back as an English requirements checklist followed by
    "Let me analyze the data:". Merchant names are Latin, so the test is which
    script dominates, not whether Latin appears at all.
    """
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return cyrillic > latin


def _ends_cleanly(text: str) -> bool:
    """Whether the reply looks finished rather than cut off mid-thought.

    The prompt requires a full stop on every line, which is what makes a
    missing one a reliable signal of truncation rather than of style.
    """
    tail = text.rstrip()
    return not tail or tail[-1] in ".!?"


def _trim(text: str) -> str:
    """Cut back to the last complete sentence.

    Two ways a reply arrives unfinished: longer than we will send, or stopped
    by max_tokens wherever the generation happened to be. The first live answer
    ended "Цифрові товари +393 % (головне — під", which reads as a bug rather
    than as brevity.
    """
    text = (text or "").strip()
    clipped = (text[:MAX_ANALYSIS_CHARS] if len(text) > MAX_ANALYSIS_CHARS
               else text).rstrip()
    if _ends_cleanly(clipped):
        return clipped
    # Cut back to the last completed sentence. Dropping the unfinished tail
    # LINE instead threw away all three actions when a model put them on one
    # line - this keeps every complete one and loses only the fragment.
    stop = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
    # No boundary to cut back to: a fragment beats nothing.
    return clipped[:stop + 1] if stop > 0 else clipped


def _known_numbers(payload) -> set:
    found = set()
    if isinstance(payload, dict):
        for value in payload.values():
            found |= _known_numbers(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found |= _known_numbers(value)
    elif isinstance(payload, bool):
        pass
    elif isinstance(payload, (int, float)):
        found.add(round(abs(float(payload))))
    elif isinstance(payload, str):
        for token in re.findall(r"\d[\d\s]*", payload):
            found.add(round(abs(float("".join(token.split())))))
    return found


def _check_numbers(text: str, payload: dict) -> None:
    """Warn when the model states a figure the input never contained.

    Logged rather than suppressed: the model is entitled to say "вдвічі" or
    "10%", and discarding a useful paragraph over an unrecognised number would
    cost more than it saves. Arithmetic itself is Python's job, and the prompt
    forbids the model from doing any.
    """
    try:
        known = _known_numbers(payload)
        invented = set()
        for token in re.findall(r"\d[\d\s]*\d|\d", text):
            # str.split() drops every Unicode space; str.replace(" ", "") only
            # drops the ASCII one, and a live model wrote "56\u202f246".
            value = round(float("".join(token.split())))
            if value >= NUMBER_CHECK_FLOOR and value not in known:
                # Off-by-one from independent rounding is not an invention.
                if not any(abs(value - k) <= 1 for k in known):
                    invented.add(value)
    except Exception as error:
        # A diagnostic must never cost the reader the answer.
        logger.warning(f'Could not check the figures ({error.__class__.__name__})')
        return
    if invented:
        logger.warning(f'Analysis states figures absent from the input: '
                       f'{sorted(invented)}')


def _ask(model: str, payload: dict, api_key: str, timeout: int,
         system: str = None, max_tokens: int = None):
    """(text, status). status is the HTTP code when known, else None."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens or MAX_TOKENS,
        # Reasoning models otherwise put their scratchpad in `content`.
        "reasoning": {"exclude": True},
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}
    try:
        raw = _post(CHAT_URL, body, headers, timeout)
        choices = json.loads(raw.decode()).get("choices") or []
        return (choices[0]["message"]["content"] if choices else None), None
    except Exception as error:
        status = getattr(error, "code", None)
        message = _redact(str(error), api_key)
        logger.info(f'{model} did not answer ({error.__class__.__name__}): {message}')
        return None, status


def analyze(payload: dict, api_key: str, prefer: str = "",
            timeout: int = TIMEOUT,
            budget: float = TOTAL_BUDGET) -> tuple[str, str] | None:
    """Ask the first free model that actually answers. None if none does.

    Availability is decided by a successful response, not by presence in the
    catalogue: free endpoints return 429 routinely. Capped at three attempts -
    walking a whole list at timeout would cost eight minutes of dyno time for a
    report nobody is waiting on.
    """
    if not api_key:
        logger.info('OPENROUTER_API_KEY not set; skipping the analysis')
        return None

    deadline = time.monotonic() + budget
    candidates = free_models(timeout)
    if prefer and prefer in candidates:
        candidates = [prefer] + [c for c in candidates if c != prefer]

    for model in candidates[:MAX_ATTEMPTS]:
        left = deadline - time.monotonic()
        if left <= 0:
            logger.warning('Analysis budget spent; the digest goes without one')
            return None
        text, status = _ask(model, payload, api_key, min(timeout, left))
        if status == 401:
            logger.warning('OpenRouter rejected the key; not trying other models')
            return None
        text = _trim(text or "")
        if not text:
            # _ask has already logged why when it knows; this covers a reply
            # that arrived successfully but held nothing.
            if status is None:
                logger.info(f'{model} returned an empty reply; trying the next')
            continue
        if not _is_the_answer(text):
            logger.info(f'{model} answered in the wrong shape, starting '
                        f'{text[:60]!r}; trying the next')
            continue
        _check_numbers(text, payload)
        if "варто зробити" not in text:
            # Not a rejection: the observations are still worth reading, and a
            # free model that follows format is not guaranteed. Logged so it is
            # visible how often the recommendations go missing.
            logger.info(f'{model} returned no recommendations section')
        return text, model

    logger.warning('No free model answered; the digest goes without an analysis')
    return None


MAX_TEXT_CHARS = 200

# A category label must fit Telegram's callback_data alongside a 16-character
# transaction id and a colon.
MAX_LABEL_BYTES = 64 - 17
RESERVED_LABELS = ("Інше", UNKNOWN)

# Naming one category is a far smaller job than writing an analysis, and the
# owner is waiting on this one - it happens between their message and the bot's
# reply, not on a schedule.
CATEGORY_BUDGET = 60

CATEGORY_PROMPT = (
    "Ти класифікуєш витрату за описом власника. "
    "Тобі дають опис, назву контрагента і список наявних категорій. "
    "Якщо якась із наявних категорій підходить — поверни ЇЇ ТОЧНУ назву. "
    "Якщо жодна не підходить — придумай коротку нову назву українською, "
    "не довшу за 20 символів. "
    "Поверни РІВНО ОДИН рядок з назвою категорії, без пояснень і розділових "
    "знаків."
)


# A category is a short Ukrainian noun phrase. "Кафе та ресторани" is three
# words; anything longer is a sentence, not a label.
MAX_LABEL_WORDS = 4


def _looks_like_a_label(line: str) -> bool:
    """Whether a reply is a category or the model talking about the task.

    A live model answered this prompt with "The user wants to categorize a
    transaction" - the system prompt paraphrased back at us. It was short
    enough to pass every length check, was offered to the owner, confirmed,
    and became a real category on a real row. Categories here are Ukrainian,
    so the test is which script dominates - the same test the digest uses to
    spot a model returning its scratchpad instead of an answer.
    """
    cyrillic = sum(1 for ch in line if "Ѐ" <= ch <= "ӿ")
    latin = sum(1 for ch in line if ch.isascii() and ch.isalpha())
    return cyrillic > latin and len(line.split()) <= MAX_LABEL_WORDS


def _usable_label(text: str) -> str | None:
    """The first line of a reply, if it can serve as a category label."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    line = stripped.splitlines()[0].strip().strip('"«».:;- ')
    if not line or line in RESERVED_LABELS:
        return None
    if len(line.encode()) > MAX_LABEL_BYTES:
        logger.info(f'Proposed label {line!r} is too long for a button')
        return None
    if not _looks_like_a_label(line):
        logger.info(f'Proposed label {line!r} reads as prose, not a category')
        return None
    return line


def propose_category(text: str, merchant: str, choices: list, api_key: str,
                     timeout: int = TIMEOUT,
                     budget: float = CATEGORY_BUDGET) -> str | None:
    """Name a category for the owner's description, or None.

    Reached only when local matching failed, so this is the exceptional path
    rather than the common one.
    """
    if not api_key:
        logger.info('OPENROUTER_API_KEY not set; cannot propose a category')
        return None

    payload = {
        "опис": (text or "")[:MAX_TEXT_CHARS],
        "контрагент": scrub_merchant(merchant),
        "наявні_категорії": list(choices or ()),
    }
    deadline = time.monotonic() + budget
    for model in free_models(timeout)[:MAX_ATTEMPTS]:
        left = deadline - time.monotonic()
        if left <= 0:
            logger.warning('Category proposal budget spent')
            return None
        reply, status = _ask(model, payload, api_key, min(timeout, left),
                             system=CATEGORY_PROMPT, max_tokens=32)
        if status == 401:
            logger.warning('OpenRouter rejected the key')
            return None
        label = _usable_label(reply or "")
        if label:
            return label
    logger.warning('No free model proposed a usable category')
    return None

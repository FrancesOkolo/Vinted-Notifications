"""AI-assisted deal evaluation via the OpenAI Responses API.

For queries with AI evaluation enabled, each new item's details and photo are
sent to an OpenAI model, which estimates a condition-aware benchmark price.
The application then deterministically calculates the saving and assigns the
user's verdict — Great / Good / Don't buy.

Evaluation is deliberately separate from the primary item-alert path.  This
module raises small, typed exceptions so the durable worker can retry transient
failures without ever delaying or losing the original alert.
"""

import html
import json
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit

import requests

_ENDPOINT = "https://api.openai.com/v1/responses"
_TIMEOUT = (10, 60)  # (connect, read)
_DEFAULT_MODEL = "gpt-5.6-terra"
_MAX_REASON_CHARS = 160
_MAX_BENCHMARK_BASIS_CHARS = 60
_MAX_SOURCE_LINKS = 3
_MAX_SOURCE_TITLE_CHARS = 72
_MAX_SOURCE_URL_CHARS = 512
_MAX_SOURCE_BLOCK_CHARS = 1800
_TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429}
_GOOD_MIN_SAVING_PERCENT = Decimal("50")
_EXCELLENT_ABOVE_SAVING_PERCENT = Decimal("65")

_LABELS = {
    "excellent": "\U0001f525 <b>AI: GREAT DEAL</b>",
    "good": "✅ <b>AI: GOOD DEAL</b>",
    "dont_buy": "⛔ <b>AI: DON'T BUY</b>",
}

_SYSTEM_PROMPT = (
    "You are a savvy UK reseller who knows brand pricing. Search the web for "
    "live UK price evidence for the same product, or the closest genuinely "
    "comparable product. Use at least two independent sources when available. "
    "If NEW (new with tags / new without tags), prefer the official brand and "
    "recognised UK retailers; if USED (very good / good / satisfactory), "
    "prefer recent sold/completed resale evidence or second-hand listings in "
    "similar condition. Do not use the target Vinted listing as evidence and "
    "do not try to discover its asking price. Never invent evidence. Estimate "
    "a condition-aware benchmark independently from the sources and return it "
    "as a positive number in the listing's currency, without a currency "
    "symbol. Reply ONLY as compact JSON: "
    '{"benchmark_price":120,"benchmark_currency":"GBP",'
    '"benchmark_basis":"<=6 words, e.g. typical used resale"}. Do not choose a '
    "verdict. The application applies the user rule "
    "deterministically: under 50% saving = don't buy; 50% through exactly 65% "
    "saving = good; strictly over 65% saving = great."
)

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "benchmark_price": {"type": "number"},
        "benchmark_currency": {"type": "string"},
        "benchmark_basis": {"type": "string"},
    },
    "required": ["benchmark_price", "benchmark_currency", "benchmark_basis"],
    "additionalProperties": False,
}


class AIDealEvaluationError(RuntimeError):
    """Base class for failures safe to expose in operational logs."""

    retryable = False


class AIConfigurationError(AIDealEvaluationError):
    """The evaluator cannot run until its local configuration is corrected."""


class AITransientError(AIDealEvaluationError):
    """A bounded retry may succeed (network, throttling, or temporary service)."""

    retryable = True


class AIPermanentError(AIDealEvaluationError):
    """Retrying the same request will not fix the failure."""


def _api_key():
    return (os.environ.get("VN_OPENAI_API_KEY") or "").strip()


def _model():
    configured = (os.environ.get("VN_OPENAI_MODEL") or "").strip()
    return configured or _DEFAULT_MODEL


def is_configured():
    """True when an OpenAI API key is available."""
    return bool(_api_key())


def _json_object(raw_json):
    """Return one JSON object, tolerating a flat object inside wrapper text."""
    if not isinstance(raw_json, str):
        return None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        match = re.search(r"\{[^{}]*\}", raw_json or "")
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def _source_record(value):
    """Return one safe HTTP(S) source record from API metadata."""
    if not isinstance(value, dict):
        return None
    nested = value.get("url_citation")
    if isinstance(nested, dict):
        value = nested
    url = str(value.get("url") or "").strip()
    if (
        not url
        or len(url) > _MAX_SOURCE_URL_CHARS
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or character == "\\"
            for character in url
        )
    ):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    title = " ".join(str(value.get("title") or "").split())
    if not title:
        title = hostname.lower().removeprefix("www.")
    if len(title) > _MAX_SOURCE_TITLE_CHARS:
        title = title[: _MAX_SOURCE_TITLE_CHARS - 1].rstrip() + "…"
    return {"url": url, "title": title}


def _response_sources(data):
    """Extract cited/consulted web sources from Responses API metadata."""
    if not isinstance(data, dict):
        return []
    output = data.get("output") or []
    if not isinstance(output, (list, tuple)):
        return []
    sources = []
    seen = set()

    def add(value):
        source = _source_record(value)
        if source is None or source["url"] in seen:
            return
        seen.add(source["url"])
        sources.append(source)

    # Cited URLs are the most directly relevant evidence, so prefer them.
    for entry in output:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        for chunk in entry.get("content") or []:
            if not isinstance(chunk, dict):
                continue
            for annotation in chunk.get("annotations") or []:
                if (
                    isinstance(annotation, dict)
                    and annotation.get("type") == "url_citation"
                ):
                    add(annotation)

    # The included sources field contains every URL consulted by web search.
    for entry in output:
        if not isinstance(entry, dict) or entry.get("type") != "web_search_call":
            continue
        action = entry.get("action")
        if not isinstance(action, dict):
            continue
        for source in action.get("sources") or []:
            add(source)

    return sources[:_MAX_SOURCE_LINKS]


def _format_sources(sources):
    """Render a short, clickable Telegram-HTML source list."""
    heading = "<b>Sources checked:</b>"
    links = []
    for source in sources or []:
        safe = _source_record(source)
        if safe is None:
            continue
        number = len(links) + 1
        url = html.escape(safe["url"], quote=True)
        title = html.escape(safe["title"])
        link = f'• <a href="{url}">{number}. {title}</a>'
        candidate = f"{heading}\n" + "\n".join([*links, link])
        if len(candidate) > _MAX_SOURCE_BLOCK_CHARS:
            break
        links.append(link)
        if len(links) >= _MAX_SOURCE_LINKS:
            break
    if not links:
        return ""
    return f"{heading}\n" + "\n".join(links)


def format_verdict(raw_json, sources=None):
    """Turn verdict JSON into a Telegram-HTML rating line, or None."""
    data = _json_object(raw_json)
    if data is None:
        return None
    verdict = (
        str(data.get("verdict", "")).strip().lower().replace("-", "_").replace(" ", "_")
    )
    label = _LABELS.get(verdict)
    if not label:
        return None
    # Truncate before escaping.  Slicing the escaped string can split an entity
    # such as ``&lt;`` and make Telegram reject the entire HTML message.
    reason_text = str(data.get("reason", "")).strip()
    if len(reason_text) > _MAX_REASON_CHARS:
        reason_text = reason_text[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    reason = html.escape(reason_text)
    source_block = _format_sources(sources)
    note = (
        "AI estimate based on the sources below."
        if source_block
        else "AI opinion, check it yourself."
    )
    result = (
        f"{label}\n<i>{reason} — {note}</i>" if reason else f"{label}\n<i>{note}</i>"
    )
    return f"{result}\n{source_block}" if source_block else result


def _decimal_amount(value, *, allow_zero):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
        return None
    return amount


def _compact_decimal(value, places="0.01"):
    rounded = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(rounded, "f").rstrip("0").rstrip(".") or "0"


def _money_text(value, currency):
    code = str(currency or "").strip().upper()
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(code)
    amount = _compact_decimal(value)
    return f"{symbol}{amount}" if symbol else f"{amount} {code or 'currency'}"


def _currency_code(value):
    code = str(value or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else None


def _saving_verdict(asking, benchmark):
    """Apply Awele's exact, deterministic deal thresholds."""
    saved = benchmark - asking
    if saved * Decimal("100") > (benchmark * _EXCELLENT_ABOVE_SAVING_PERCENT):
        return "excellent"
    if saved * Decimal("100") >= benchmark * _GOOD_MIN_SAVING_PERCENT:
        return "good"
    return "dont_buy"


def format_evaluation(raw_json, asking_price, currency, sources=None):
    """Classify one AI benchmark using the user's non-negotiable thresholds."""
    data = _json_object(raw_json)
    if data is None:
        return None
    benchmark = _decimal_amount(data.get("benchmark_price"), allow_zero=False)
    asking = _decimal_amount(asking_price, allow_zero=True)
    listing_currency = _currency_code(currency)
    benchmark_currency = _currency_code(data.get("benchmark_currency"))
    if (
        benchmark is None
        or asking is None
        or not listing_currency
        or benchmark_currency != listing_currency
    ):
        return None

    saving_percent = ((benchmark - asking) / benchmark) * Decimal("100")
    verdict = _saving_verdict(asking, benchmark)
    basis = " ".join(str(data.get("benchmark_basis") or "benchmark").split())
    basis = basis[:_MAX_BENCHMARK_BASIS_CHARS].rstrip() or "benchmark"
    if saving_percent >= 0:
        comparison = f"{_compact_decimal(saving_percent)}% saving"
    else:
        comparison = f"{_compact_decimal(abs(saving_percent))}% above benchmark"
    reason = (
        f"{_money_text(asking, listing_currency)} vs "
        f"{_money_text(benchmark, listing_currency)} "
        f"{basis} — {comparison}"
    )
    return format_verdict(
        json.dumps({"verdict": verdict, "reason": reason}),
        sources=sources,
    )


def evaluate(item):
    """Return an HTML rating line for one item.

    Raises a typed :class:`AIDealEvaluationError` instead of swallowing the
    problem.  The durable worker owns retry/backoff policy; callers must never
    run this function inline with the primary item notification.
    """
    api_key = _api_key()
    if not api_key:
        raise AIConfigurationError("VN_OPENAI_API_KEY is not configured.")
    asking_price = _decimal_amount(_item_value(item, "price"), allow_zero=True)
    if asking_price is None:
        raise AIPermanentError("The listing price is unavailable or invalid.")
    currency = _currency_code(_item_value(item, "currency"))
    if currency is None:
        raise AIPermanentError("The listing currency is unavailable or invalid.")

    details = (
        f"Brand: {_item_text(item, 'brand_title')}\n"
        f"Title: {_item_text(item, 'title')}\n"
        f"Condition: {_item_text(item, 'condition')}\n"
        f"Benchmark currency: {currency}"
    )
    user_content = [{"type": "input_text", "text": details}]
    photo = _item_value(item, "photo")
    if photo:
        user_content.append({"type": "input_image", "image_url": str(photo)})
    payload = {
        "model": _model(),
        "input": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vinted_deal_evaluation",
                "strict": True,
                "schema": _VERDICT_SCHEMA,
            }
        },
        "tools": [
            {
                "type": "web_search",
                "user_location": {
                    "type": "approximate",
                    "country": "GB",
                },
            }
        ],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        # The request is single-turn and contains a public listing photo.  Do
        # not retain server-side response state for a conversation we never use.
        "store": False,
        # Reasoning models spend output tokens on internal reasoning, so leave
        # headroom or the final JSON verdict can be truncated.
        "max_output_tokens": 1500,
    }

    try:
        response = requests.post(
            _ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=_TIMEOUT,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        raise AITransientError(
            "OpenAI request timed out or could not connect."
        ) from error
    except requests.exceptions.RequestException as error:
        raise AITransientError(
            "OpenAI request failed before a response arrived."
        ) from error

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in _TRANSIENT_HTTP_STATUSES or status_code >= 500:
        raise AITransientError(
            f"OpenAI Responses API temporarily returned HTTP {status_code}."
        )
    if status_code < 200 or status_code >= 300:
        raise AIPermanentError(
            f"OpenAI Responses API rejected the request with HTTP {status_code}."
        )

    try:
        response_data = response.json()
    except (TypeError, ValueError) as error:
        raise AITransientError("OpenAI returned an invalid JSON response.") from error

    sources = _response_sources(response_data)
    if not sources:
        raise AITransientError("OpenAI returned no comparison sources.")
    rating = format_evaluation(
        _response_text(response_data),
        asking_price,
        currency,
        sources=sources,
    )
    if not rating:
        raise AITransientError("OpenAI returned no valid deal verdict.")
    return rating


def _item_value(item, name):
    """Read one snapshot field from either a mapping or item-like object."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _item_text(item, name):
    value = _item_value(item, name)
    if value is None or not str(value).strip():
        return "unknown"
    return str(value)


def _response_text(data):
    """Extract the assistant's final text from an OpenAI Responses API result."""
    if not isinstance(data, dict):
        return ""
    aggregated = data.get("output_text")
    if isinstance(aggregated, str) and aggregated.strip():
        return aggregated
    output = data.get("output") or []
    if not isinstance(output, (list, tuple)):
        return ""
    parts = []
    for entry in output:
        if isinstance(entry, dict) and entry.get("type") == "message":
            for chunk in entry.get("content") or []:
                text = chunk.get("text") if isinstance(chunk, dict) else None
                if text:
                    parts.append(text)
    return "\n".join(parts)

"""AI-assisted deal evaluation via the OpenAI Responses API.

For queries with AI evaluation enabled, each new item's details and photo are
sent to a small OpenAI model, which returns a verdict — Excellent / Good /
Don't buy — with a one-line reason based on the item, its condition and typical
resale value.

Evaluation is deliberately separate from the primary item-alert path.  This
module raises small, typed exceptions so the durable worker can retry transient
failures without ever delaying or losing the original alert.
"""

import html
import json
import os
import re

import requests

_ENDPOINT = "https://api.openai.com/v1/responses"
_TIMEOUT = (10, 60)  # (connect, read)
_DEFAULT_MODEL = "gpt-5.6-terra"
_MAX_REASON_CHARS = 160
_TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429}

_LABELS = {
    "excellent": "\U0001f525 <b>AI: EXCELLENT DEAL</b>",
    "good": "✅ <b>AI: GOOD DEAL</b>",
    "dont_buy": "⛔ <b>AI: DON'T BUY</b>",
}

_SYSTEM_PROMPT = (
    "You are a savvy UK reseller who knows brand pricing. Using ONLY your own "
    "knowledge (do NOT browse the web), estimate the item's benchmark price "
    "from its brand, title, condition and photo: if NEW (new with tags / new "
    "without tags) use its typical UK RETAIL (new) price; if USED (very good / "
    "good / satisfactory) use its typical second-hand resale price for that "
    "condition. Judge the asking price against that benchmark. Reply ONLY as "
    "compact JSON: "
    '{"verdict":"excellent|good|dont_buy","reason":"<=16 words; state the '
    'benchmark figure and the discount"}. '
    "excellent = a clear bargain well below the benchmark; good = a fair price "
    "/ modest saving; dont_buy = around or above the benchmark, or not worth it."
)

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["excellent", "good", "dont_buy"],
        },
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
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


def format_verdict(raw_json):
    """Turn the model's JSON reply into a Telegram-HTML rating line, or None.

    If the whole string is not valid JSON, extract the first flat {...} object
    as a defensive fallback for malformed model output.
    """
    if not isinstance(raw_json, str):
        return None
    data = None
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        match = re.search(r"\{[^{}]*\}", raw_json or "")
        if match:
            try:
                data = json.loads(match.group(0))
            except (TypeError, ValueError):
                data = None
    if not isinstance(data, dict):
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
    if reason:
        return f"{label}\n<i>{reason} — AI opinion, check it yourself.</i>"
    return f"{label}\n<i>AI opinion, check it yourself.</i>"


def evaluate(item):
    """Return an HTML rating line for one item.

    Raises a typed :class:`AIDealEvaluationError` instead of swallowing the
    problem.  The durable worker owns retry/backoff policy; callers must never
    run this function inline with the primary item notification.
    """
    api_key = _api_key()
    if not api_key:
        raise AIConfigurationError("VN_OPENAI_API_KEY is not configured.")

    details = (
        f"Vinted listing: {_item_text(item, 'url')}\n"
        f"Brand: {_item_text(item, 'brand_title')}\n"
        f"Title: {_item_text(item, 'title')}\n"
        f"Condition: {_item_text(item, 'condition')}\n"
        f"Asking price: {_item_text(item, 'price')} "
        f"{_item_text(item, 'currency')}"
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

    rating = format_verdict(_response_text(response_data))
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

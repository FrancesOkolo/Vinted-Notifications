"""AI-assisted deal evaluation via the OpenAI API.

For queries with AI evaluation enabled, each new item's details and photo are
sent to a small OpenAI model, which returns a verdict — Excellent / Good /
Don't buy — with a one-line reason based on the item, its condition and typical
resale value.

It is best-effort: any error (no key, timeout, bad response) simply means no
rating is shown and the alert still goes out. Only fields the scraper already
has are sent (brand, title, condition, price, currency, photo URL); no
description or personal data is transmitted.
"""
import html
import json
import os
import re

import requests

from logger import get_logger

logger = get_logger(__name__)

_ENDPOINT = "https://api.openai.com/v1/responses"
_TIMEOUT = (10, 60)  # (connect, read); web search needs a longer read timeout

_LABELS = {
    "excellent": "\U0001f525 <b>AI: EXCELLENT DEAL</b>",
    "good": "✅ <b>AI: GOOD DEAL</b>",
    "dont_buy": "⛔ <b>AI: DON'T BUY</b>",
}

_SYSTEM_PROMPT = (
    "You are a savvy UK reseller. First identify the EXACT product from its "
    "Vinted listing link, title and photo (mind the precise variant, e.g. a "
    "lampshade is not a whole lamp). Then use web search to find THAT specific "
    "product's current UK RETAIL (new) price and recent second-hand asking/sold "
    "prices. Pick the "
    "benchmark by condition: if NEW (new with tags / new without tags) use "
    "retail; if USED (very good / good / satisfactory) use the typical "
    "second-hand resale price for that condition. Judge the asking price "
    "against that benchmark. After searching, reply with ONLY compact JSON on "
    "the final line: "
    '{"verdict":"excellent|good|dont_buy","reason":"<=18 words; name the '
    'benchmark, its rough figure and the discount"}. '
    "excellent = a clear bargain below the benchmark; good = a fair price / "
    "modest saving; dont_buy = around or above the benchmark, or not worth it."
)


def _api_key():
    return (os.environ.get("VN_OPENAI_API_KEY") or "").strip()


def _model():
    return (os.environ.get("VN_OPENAI_MODEL") or "gpt-4o-mini").strip()


def is_configured():
    """True when an OpenAI API key is available."""
    return bool(_api_key())


def format_verdict(raw_json):
    """Turn the model's JSON reply into a Telegram-HTML rating line, or None.

    The reply may include prose (e.g. web-search notes) around the JSON, so if
    the whole string is not valid JSON, extract the first flat {...} object.
    """
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
        str(data.get("verdict", ""))
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    label = _LABELS.get(verdict)
    if not label:
        return None
    reason = html.escape(str(data.get("reason", "")).strip())[:160]
    if reason:
        return f"{label}\n<i>{reason} — AI opinion, check it yourself.</i>"
    return f"{label}\n<i>AI opinion, check it yourself.</i>"


def evaluate(item):
    """Return an HTML rating line for one item, or None on any problem."""
    api_key = _api_key()
    if not api_key:
        return None
    try:
        details = (
            f"Vinted listing: {getattr(item, 'url', None) or 'unknown'}\n"
            f"Brand: {getattr(item, 'brand_title', None) or 'unknown'}\n"
            f"Title: {item.title}\n"
            f"Condition: {getattr(item, 'condition', None) or 'unknown'}\n"
            f"Asking price: {item.price} {item.currency}"
        )
        user_content = [{"type": "input_text", "text": details}]
        photo = getattr(item, "photo", None)
        if photo:
            user_content.append({"type": "input_image", "image_url": str(photo)})
        payload = {
            "model": _model(),
            # "high" search context: accuracy first while the evaluator is
            # being tuned (low context was cheaper but less reliable).
            "tools": [{"type": "web_search_preview", "search_context_size": "high"}],
            "input": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            # Reasoning models (gpt-5.x) spend most output tokens on internal
            # reasoning across several web searches, so leave generous room or
            # the final JSON verdict gets truncated. Unused tokens are not billed.
            "max_output_tokens": 3000,
        }
        response = requests.post(
            _ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        return format_verdict(_response_text(response.json()))
    except Exception:
        logger.warning(
            "AI deal evaluation failed; the alert will be sent without a rating.",
            exc_info=True,
        )
        return None


def _response_text(data):
    """Extract the assistant's final text from an OpenAI Responses API result."""
    aggregated = data.get("output_text")
    if isinstance(aggregated, str) and aggregated.strip():
        return aggregated
    parts = []
    for entry in data.get("output") or []:
        if entry.get("type") == "message":
            for chunk in entry.get("content") or []:
                text = chunk.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)

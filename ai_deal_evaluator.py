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

import requests

from logger import get_logger

logger = get_logger(__name__)

_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_TIMEOUT = (10, 25)  # (connect, read) seconds

_LABELS = {
    "excellent": "\U0001f525 <b>AI: EXCELLENT DEAL</b>",
    "good": "✅ <b>AI: GOOD DEAL</b>",
    "dont_buy": "⛔ <b>AI: DON'T BUY</b>",
}

_SYSTEM_PROMPT = (
    "You are a savvy UK reseller who knows brand pricing. Pick the benchmark "
    "from the item's condition: if NEW (new with tags / new without tags), use "
    "the item's RETAIL (new) price; if USED (very good / good / satisfactory), "
    "use the typical SECOND-HAND resale price for that condition. Estimate that "
    "benchmark from the brand, title, condition and photo, then judge the "
    "asking price against it. Reply ONLY as compact JSON: "
    '{"verdict":"excellent|good|dont_buy","reason":"<=16 words; name the '
    'benchmark used and the discount"}. '
    "Guide: excellent = a clear bargain well below the benchmark; good = a fair "
    "price / modest saving; dont_buy = around or above the benchmark, or not "
    "worth it. Be decisive on clear bargains."
)


def _api_key():
    return (os.environ.get("VN_OPENAI_API_KEY") or "").strip()


def _model():
    return (os.environ.get("VN_OPENAI_MODEL") or "gpt-4o-mini").strip()


def is_configured():
    """True when an OpenAI API key is available."""
    return bool(_api_key())


def format_verdict(raw_json):
    """Turn the model's JSON reply into a Telegram-HTML rating line, or None."""
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
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
            f"Brand: {getattr(item, 'brand_title', None) or 'unknown'}\n"
            f"Title: {item.title}\n"
            f"Condition: {getattr(item, 'condition', None) or 'unknown'}\n"
            f"Asking price: {item.price} {item.currency}"
        )
        user_content = [{"type": "text", "text": details}]
        photo = getattr(item, "photo", None)
        if photo:
            user_content.append(
                {"type": "image_url", "image_url": {"url": str(photo)}}
            )
        payload = {
            "model": _model(),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 60,
            "temperature": 0.2,
        }
        response = requests.post(
            _ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return format_verdict(content)
    except Exception:
        logger.warning(
            "AI deal evaluation failed; the alert will be sent without a rating.",
            exc_info=True,
        )
        return None

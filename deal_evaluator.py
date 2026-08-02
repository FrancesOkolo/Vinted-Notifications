"""Transparent, per-query listing-price evaluation.

The evaluator deliberately does not claim to estimate market value.  It only
compares the item's advertised price with ceilings chosen by the user for the
query that found it.  Postage, Vinted buyer-protection fees, authenticity and
item condition remain outside this calculation.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class DealRating:
    """The stable rating snapshot prepended to a notification."""

    code: str
    label: str
    detail: str

    def as_html(self):
        """Return Telegram-safe HTML containing only trusted constant text."""
        return f"<b>{self.label}</b>\n<i>{self.detail}</i>"


_RATINGS = {
    "excellent": DealRating(
        "excellent",
        "\U0001f525 EXCELLENT DEAL",
        "Listing-price rating only; postage and buyer fees are excluded.",
    ),
    "good": DealRating(
        "good",
        "\u2705 GOOD DEAL",
        "Listing-price rating only; postage and buyer fees are excluded.",
    ),
    "above_limit": DealRating(
        "above_limit",
        "\u26d4 DON'T BUY \u2014 ABOVE YOUR LIMIT",
        "Listing-price rating only; condition and authenticity still need checking.",
    ),
    "not_rated": DealRating(
        "not_rated",
        "\u26aa NOT RATED",
        "The listing price or currency could not be compared safely.",
    ),
}


def decimal_amount(value):
    """Return a finite, non-negative Decimal, or ``None`` for invalid input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def evaluate_listing_price(price, currency, preferences):
    """Rate ``price`` using one query's saved preferences.

    ``None`` means the feature is disabled for the query.  When it is enabled,
    malformed settings or a currency mismatch intentionally return NOT RATED
    instead of suppressing the alert or guessing.
    """
    preferences = preferences or {}
    if not preferences.get("deal_evaluator_enabled", False):
        return None

    expected_currency = str(preferences.get("deal_currency") or "").strip().upper()
    actual_currency = str(currency or "").strip().upper()
    item_price = decimal_amount(price)
    excellent_max = decimal_amount(preferences.get("deal_excellent_max"))
    good_max = decimal_amount(preferences.get("deal_good_max"))

    if (
        item_price is None
        or excellent_max is None
        or good_max is None
        or excellent_max > good_max
        or not expected_currency
        or actual_currency != expected_currency
    ):
        return _RATINGS["not_rated"]
    if item_price <= excellent_max:
        return _RATINGS["excellent"]
    if item_price <= good_max:
        return _RATINGS["good"]
    return _RATINGS["above_limit"]


def prepend_deal_rating(content, price, currency, preferences):
    """Prepend a rating snapshot without altering disabled notifications."""
    rating = evaluate_listing_price(price, currency, preferences)
    if rating is None:
        return content, None
    return f"{rating.as_html()}\n\n{content}", rating

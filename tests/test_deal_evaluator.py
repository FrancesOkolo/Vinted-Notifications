from decimal import Decimal

import pytest

from deal_evaluator import decimal_amount, evaluate_listing_price, prepend_deal_rating


def _preferences(**overrides):
    values = {
        "deal_evaluator_enabled": True,
        "deal_excellent_max": "40.00",
        "deal_good_max": "65.00",
        "deal_currency": "GBP",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("0", Decimal("0")),
        ("12.30", Decimal("12.30")),
        (12, Decimal("12")),
        ("-1", None),
        ("NaN", None),
        ("Infinity", None),
        ("not-a-price", None),
        (None, None),
        (True, None),
    ],
)
def test_decimal_amount_accepts_only_finite_non_negative_values(price, expected):
    assert decimal_amount(price) == expected


@pytest.mark.parametrize(
    ("price", "expected_code"),
    [
        ("39.99", "excellent"),
        ("40.00", "excellent"),
        ("40.01", "good"),
        ("65.00", "good"),
        ("65.01", "above_limit"),
    ],
)
def test_evaluator_uses_inclusive_user_price_ceilings(price, expected_code):
    assert evaluate_listing_price(price, "GBP", _preferences()).code == expected_code


def test_evaluator_is_disabled_unless_selected_for_the_query():
    assert evaluate_listing_price("10", "GBP", _preferences(deal_evaluator_enabled=False)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"deal_excellent_max": None},
        {"deal_good_max": None},
        {"deal_excellent_max": "70", "deal_good_max": "60"},
        {"deal_currency": ""},
    ],
)
def test_invalid_preferences_still_notify_as_not_rated(overrides):
    assert evaluate_listing_price("10", "GBP", _preferences(**overrides)).code == "not_rated"


def test_currency_mismatch_is_not_rated_instead_of_guessed():
    assert evaluate_listing_price("10", "EUR", _preferences()).code == "not_rated"


def test_rating_is_a_snapshot_prepended_to_existing_notification():
    content, rating = prepend_deal_rating(
        "Title: Pooky lamp",
        "35",
        "GBP",
        _preferences(),
    )

    assert rating.code == "excellent"
    assert content.startswith("<b>\U0001f525 EXCELLENT DEAL</b>")
    assert "listing-price rating only" in content.lower()
    assert content.endswith("Title: Pooky lamp")


def test_disabled_evaluator_preserves_notification_byte_for_byte():
    original = "<b>Existing message</b>"
    content, rating = prepend_deal_rating(
        original,
        "35",
        "GBP",
        _preferences(deal_evaluator_enabled=False),
    )

    assert content == original
    assert rating is None

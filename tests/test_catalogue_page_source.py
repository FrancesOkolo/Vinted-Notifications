import json
from urllib.parse import parse_qs, urlsplit

import pytest

from pyVintedVN.items.item import Item
from pyVintedVN.items.items import (
    CataloguePageParseError,
    Items,
    _catalogue_page_url,
    parse_catalogue_page,
)


def _product(item_id, title, *, amount="25.00"):
    return {
        "id": item_id,
        "productItem": {
            "id": item_id,
            "title": title,
            "url": f"/items/{item_id}-test-item",
            "price": {"amount": amount, "currencyCode": "GBP"},
            "thumbnailUrl": f"https://images.example/{item_id}.webp",
            "itemBox": {
                "firstLine": "Tom Raffield",
                "secondLine": "Very good",
            },
        },
        "catalogTracking": {"contentSource": "search"},
    }


def _page(*wrappers):
    stream = json.dumps(
        {"items": {"items": list(wrappers), "pagination": {"currentPage": 1}}},
        separators=(",", ":"),
    )
    payload = json.dumps([1, stream], separators=(",", ":"))
    return f"<html><script>self.__next_f.push({payload})</script></html>"


def test_catalogue_page_parser_preserves_order_and_result_limit():
    data = parse_catalogue_page(
        _page(_product(300, "Newest"), _product(200, "Older")),
        "www.vinted.co.uk",
        1,
    )

    assert len(data) == 1
    assert data[0] == {
        "id": 300,
        "title": "Newest",
        "brand_title": "Tom Raffield",
        "status_title": "Very good",
        "description": None,
        "price": {"amount": "25.00", "currency_code": "GBP"},
        "photo": {
            "url": "https://images.example/300.webp",
            "high_resolution": {"timestamp": None},
        },
        "url": "https://www.vinted.co.uk/items/300-test-item",
    }


def test_catalogue_page_parser_accepts_valid_empty_results():
    assert parse_catalogue_page(_page(), "www.vinted.co.uk", 20) == []


def test_parser_ignores_product_objects_outside_catalogue_array():
    page = _page(_product(10, "Search result"))
    unrelated = json.dumps(
        [1, json.dumps(_product(99, "Recommendation"), separators=(",", ":"))]
    )
    page += f"<script>self.__next_f.push({unrelated})</script>"
    assert [
        item["id"] for item in parse_catalogue_page(page, "www.vinted.co.uk", 20)
    ] == [10]


def test_parser_rejects_partial_invalid_result_window():
    broken = _product(11, "Broken")
    del broken["productItem"]["price"]
    with pytest.raises(CataloguePageParseError):
        parse_catalogue_page(
            _page(_product(10, "Valid"), broken), "www.vinted.co.uk", 20
        )


def test_catalogue_page_parser_rejects_unrecognisable_success_page():
    with pytest.raises(CataloguePageParseError):
        parse_catalogue_page("<html>Sign in</html>", "www.vinted.co.uk", 20)


def test_catalogue_page_url_keeps_filters_and_forces_requested_page():
    url = _catalogue_page_url(
        "https://www.vinted.co.uk/api/v2/catalog/items?"
        "brand_ids%5B%5D=8113368&order=newest_first&page=6&_rsc=old",
        2,
    )
    parts = urlsplit(url)

    assert parts.path == "/catalog"
    assert parse_qs(parts.query) == {
        "brand_ids[]": ["8113368"],
        "order": ["newest_first"],
        "page": ["2"],
    }


def test_items_search_uses_public_page_and_unknown_timestamp(monkeypatch):
    import pyVintedVN.items.items as items_module

    calls = []

    class Response:
        text = _page(_product(123, "Steam-bent lamp"))

        def raise_for_status(self):
            return None

    class PageRequester:
        def set_locale(self, locale):
            calls.append(("locale", locale))

        def get_catalogue_page(self, url):
            calls.append(("page", url))
            return Response()

    monkeypatch.setattr(items_module, "requester", PageRequester())

    results = Items().search(
        "https://www.vinted.co.uk/catalog?search_text=lamp&order=newest_first",
        nbr_items=20,
    )

    assert calls == [
        ("locale", "www.vinted.co.uk"),
        (
            "page",
            "https://www.vinted.co.uk/catalog?search_text=lamp&order=newest_first",
        ),
    ]
    assert len(results) == 1
    assert isinstance(results[0], Item)
    assert results[0].raw_timestamp is None
    assert results[0].is_new_item() is False

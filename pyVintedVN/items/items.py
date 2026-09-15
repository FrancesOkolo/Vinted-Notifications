import json as jsonlib
import re

from pyVintedVN.items.item import Item
from pyVintedVN.requester import requester
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from requests.exceptions import HTTPError
from typing import List, Dict, Optional

_NEXT_DATA_PUSH_PATTERN = re.compile(
    r"self\.__next_f\.push\((\[.*?\])\)\s*;?\s*</script>",
    re.DOTALL,
)
_CATALOGUE_ITEMS_MARKER = '"items":{"items":['


class CataloguePageParseError(ValueError):
    """Raised when a successful Vinted page lacks catalogue result data."""


def _catalogue_page_url(url, page):
    """Return the public catalogue URL while preserving all saved filters."""
    parts = urlsplit(url)
    path = parts.path
    if path.rstrip("/") == "/api/v2/catalog/items":
        path = "/catalog"
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"page", "_rsc"}
    ]
    if int(page or 1) > 1:
        pairs.append(("page", str(int(page))))
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(pairs), ""))


def _next_data_stream(page_html):
    chunks = []
    for match in _NEXT_DATA_PUSH_PATTERN.finditer(str(page_html or "")):
        try:
            payload = jsonlib.loads(match.group(1))
        except (TypeError, ValueError, jsonlib.JSONDecodeError):
            continue
        if len(payload) > 1 and isinstance(payload[1], str):
            chunks.append(payload[1])
    return "".join(chunks)


def _clean_product_text(value):
    if value is None or str(value).startswith("$undefined"):
        return ""
    return str(value).strip()


def _legacy_item_data(product, locale):
    """Map Vinted's current page model to the established Item contract."""
    item_box = product.get("itemBox")
    item_box = item_box if isinstance(item_box, dict) else {}
    price = product.get("price")
    price = price if isinstance(price, dict) else {}
    amount = price.get("amount")
    currency = price.get("currencyCode")
    item_id = product.get("id")
    title = _clean_product_text(product.get("title"))
    relative_url = _clean_product_text(product.get("url"))
    if item_id in (None, "") or not title or amount is None or not currency:
        return None

    photo_url = _clean_product_text(product.get("thumbnailUrl")) or None
    if not photo_url:
        photos = product.get("photos")
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict) and photo.get("url"):
                    photo_url = str(photo["url"])
                    break

    return {
        "id": item_id,
        "title": title,
        "brand_title": _clean_product_text(item_box.get("firstLine")),
        "status_title": _clean_product_text(item_box.get("secondLine")),
        "description": None,
        "price": {
            "amount": str(amount),
            "currency_code": str(currency).upper(),
        },
        "photo": {
            "url": photo_url,
            # The new server-rendered catalogue model does not expose the old
            # photo timestamp. None prevents a first observation from treating
            # the whole result window as newly listed.
            "high_resolution": {"timestamp": None},
        },
        "url": urljoin(f"https://{locale}/", relative_url),
    }


def parse_catalogue_page(page_html, locale, limit):
    """Extract ordered listing cards from Vinted's server-rendered page data."""
    stream = _next_data_stream(page_html)
    if _CATALOGUE_ITEMS_MARKER not in stream:
        raise CataloguePageParseError(
            "Vinted catalogue page did not contain a recognisable items payload"
        )

    start = stream.index(_CATALOGUE_ITEMS_MARKER) + len('"items":')
    try:
        state, _ = jsonlib.JSONDecoder().raw_decode(stream, start)
    except ValueError as error:
        raise CataloguePageParseError("Incomplete catalogue payload") from error
    if not isinstance(state, dict) or not isinstance(state.get("items"), list):
        raise CataloguePageParseError("Invalid catalogue items array")
    if state.get("uiState", "SUCCESS") != "SUCCESS":
        raise CataloguePageParseError("Catalogue results are not ready")
    maximum = max(1, int(limit or 1))
    results = []
    seen_ids = set()
    for wrapper in state["items"]:
        product = wrapper.get("productItem") if isinstance(wrapper, dict) else None
        if not isinstance(product, dict):
            raise CataloguePageParseError("Unexpected catalogue result type")
        mapped = _legacy_item_data(product, locale)
        if mapped is None:
            raise CataloguePageParseError("Incomplete catalogue item")
        if str(mapped["id"]) in seen_ids:
            continue
        seen_ids.add(str(mapped["id"]))
        results.append(mapped)
        if len(results) >= maximum:
            break

    return results


class Items:
    """
    A class for searching and retrieving items from Vinted.

    This class provides methods to search for items on Vinted using a search URL
    and to parse Vinted search URLs into API parameters.

    Example:
        >>> items = Items()
        >>> results = items.search("https://www.vinted.fr/catalog?search_text=shoes")
    """

    result_source = "catalogue_page"

    def search(
        self,
        url: str,
        nbr_items: int = 20,
        page: int = 1,
        time: Optional[int] = None,
        json: bool = False,
    ) -> List[Item]:
        """
        Retrieve items from a given search URL on Vinted.

        Args:
            url (str): The URL of the search on Vinted.
            nbr_items (int, optional): Number of items to be returned. Defaults to 20.
            page (int, optional): Page number to be returned. Defaults to 1.
            time (int, optional): Timestamp to filter items by time. Defaults to None. Looks like it doesn't work though.
            json (bool, optional): Whether to return raw JSON data instead of Item objects.
                Defaults to False.

        Returns:
            List[Item]: A list of Item objects.

        Raises:
            HTTPError: If the request to the Vinted API fails.
        """
        # Extract the domain from the URL and set the locale
        locale = urlparse(url).netloc
        requester.set_locale(locale)

        try:
            # Vinted retired the anonymous catalogue JSON route in September
            # 2026. Its public catalogue page now contains the same ordered
            # listing-card data in the server-rendered Next.js response.
            response = requester.get_catalogue_page(_catalogue_page_url(url, page))
            response.raise_for_status()
            items = parse_catalogue_page(response.text, locale, nbr_items)

            # Return either Item objects or raw JSON data
            if not json:
                return [Item(_item) for _item in items]
            else:
                return items

        except HTTPError as err:
            raise err

    def parse_url(
        self, url: str, nbr_items: int = 20, page: int = 1, time: Optional[int] = None
    ) -> Dict:
        """
        Parse a Vinted search URL to get parameters for the API call.

        Args:
            url (str): The URL of the search on Vinted.
            nbr_items (int, optional): Number of items to be returned. Defaults to 20.
            page (int, optional): Page number to be returned. Defaults to 1.
            time (int, optional): Timestamp to filter items by time. Defaults to None.

        Returns:
            Dict: A dictionary of parameters for the Vinted API.
        """
        # Parse the query parameters from the URL
        queries = parse_qsl(urlparse(url).query)

        # Construct the parameters dictionary
        params = {
            "search_text": "+".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "search_text"])
            ),
            "video_game_platform_ids": ",".join(
                map(
                    str,
                    [
                        tpl[1]
                        for tpl in queries
                        if tpl[0] == "video_game_platform_ids[]"
                    ],
                )
            ),
            "catalog_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "catalog[]"])
            ),
            "color_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "color_ids[]"])
            ),
            "brand_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "brand_ids[]"])
            ),
            "size_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "size_ids[]"])
            ),
            "material_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "material_ids[]"])
            ),
            "status_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "status_ids[]"])
            ),
            "country_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "country_ids[]"])
            ),
            "city_ids": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "city_ids[]"])
            ),
            "is_for_swap": ",".join(
                map(str, [1 for tpl in queries if tpl[0] == "disposal[]"])
            ),
            "currency": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "currency"])
            ),
            "price_to": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "price_to"])
            ),
            "price_from": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "price_from"])
            ),
            "page": page,
            "per_page": nbr_items,
            "order": ",".join(
                map(str, [tpl[1] for tpl in queries if tpl[0] == "order"])
            ),
            "time": time,
        }

        return params

    # Aliases for backward compatibility
    parseUrl = parse_url

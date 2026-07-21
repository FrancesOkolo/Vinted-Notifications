from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

REMOVE_PARAMETERS = {
    "search_by_image_uuid",
    "search_by_image_id",
    "page",
    "time",
    "search_id",
    "disabled_personalization",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "referrer",
}


def _is_vinted_host(hostname: str) -> bool:
    hostname = hostname.lower().strip(".")
    return hostname == "vinted.co.uk" or hostname.endswith(".vinted.co.uk")


def normalise_vinted_url(raw_url: str) -> str:
    """
    Convert a Vinted UK catalogue or brand-page URL into a clean catalogue URL.

    Repeated filters, such as more than one size_ids[] value, are preserved.
    """
    if not isinstance(raw_url, str):
        raise TypeError("The Vinted URL must be text.")

    raw_url = raw_url.strip()

    if not raw_url:
        raise ValueError("The Vinted URL cannot be empty.")

    if raw_url.startswith(("vinted.co.uk/", "www.vinted.co.uk/")):
        raw_url = "https://" + raw_url

    parsed = urlsplit(raw_url)

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("The URL must begin with http:// or https://.")

    hostname = parsed.hostname or ""
    if not _is_vinted_host(hostname):
        raise ValueError("Only vinted.co.uk URLs are accepted.")

    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    parameters = [
        (key, value)
        for key, value in parameters
        if key.lower() not in REMOVE_PARAMETERS and value.strip() != ""
    ]

    brand_match = re.fullmatch(
        r"/brand/(\d+)(?:-[^/?#]+)?/?",
        parsed.path,
        flags=re.IGNORECASE,
    )

    if brand_match:
        brand_id = brand_match.group(1)
        parameters = [
            (key, value)
            for key, value in parameters
            if key not in {"brand_ids[]", "brand_ids"}
        ]
        parameters.append(("brand_ids[]", brand_id))
    elif parsed.path.rstrip("/") != "/catalog":
        raise ValueError(
            "Paste a Vinted catalogue-search URL or Vinted brand-page URL."
        )

    if not any(key == "currency" for key, _ in parameters):
        parameters.append(("currency", "GBP"))

    # Replace any existing order value so all alerts use newest first.
    parameters = [(key, value) for key, value in parameters if key != "order"]
    parameters.append(("order", "newest_first"))

    return "https://www.vinted.co.uk/catalog?" + urlencode(parameters)

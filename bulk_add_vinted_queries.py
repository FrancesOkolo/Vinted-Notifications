"""
Bulk-add Vinted queries through the running Vinted Notifications web interface.

Instructions:
1. Keep Vinted Notifications running.
2. Put this file in your Vinted-Notifications folder.
3. Open a SECOND PowerShell window in that folder.
4. Run: python bulk_add_vinted_queries.py
"""

from __future__ import annotations

import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from html.parser import HTMLParser


QUERIES = ['https://www.vinted.co.uk/catalog?search_text=nike+air+zoom+structure&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=stain+shark&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=cryo+glow+shark&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Agent+Provocateur+34F&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=feetup&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=De%27Longhi+Eletta&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=denim+jacket+tall&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=KeepCup+S&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Sheridan+double&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Johnstons+of+Elgin+women&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Dowsing+%26+Reynolds&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=waterpik&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Cox+%26+Cox&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=eufy+e28&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Samsung+monitor&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=lefant&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=House+of+CB&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Dr.+Martens&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Asics&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Lusso&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Beurer&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Gisela+Graham&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Cole+%26+Mason&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=brocante&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Reiss+dress&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Lanc%C3%B4me+Absolue&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Est%C3%A9e+Lauder+rich+mahogany&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=virgin+hair&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Robert+Welch&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=skirt&brand_ids%5B%5D=214728&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=abode&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=reformation+dress&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Alice+%2B+Olivia+women&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=waring&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Wandsworth&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Harvey+Norman&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=ASOS+tall+leggings&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Holland+%26+Barrett+muscle&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=EcoAir&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=finish&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Samsung+watch+ultra&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Est%C3%A9e+Lauder+deep+amber&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=NARS+zambie&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=br122&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Russell+%26+Bromley+women&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=gaggenau&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=De%27Longhi&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Launer+London&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=wacaco&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Soak+%26+Sleep&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=boadicea&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=tongue+groove&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?brand_ids%5B%5D=214728&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=SkinCeuticals&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=ProCook&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=LK+Bennett+boots&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Philips+flosser&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Christy%27s+bedding+set&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=nikon+lenses+vr&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=La+Roche+Posay+Duo+%2B+M&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Karen+Millen+tall+outerwear&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Boadicea+blue&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Nordic+Ware&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Soul+Journeys+figurine&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=robot+vacuum&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Swan+Dirtmaster&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Robert+Welch+cutlery&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=denim+jacket+cropped&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Breville&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=shorts&brand_ids%5B%5D=214728&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=U-Scan&brand_ids%5B%5D=277757&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?brand_ids%5B%5D=1803016&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=blenders+KitchenAid&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?brand_ids%5B%5D=277757&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?brand_ids%5B%5D=394788&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?brand_ids%5B%5D=313078&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Tapo&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=dlx&brand_ids%5B%5D=26963&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=Clarke+%26+Clarke&currency=GBP&order=newest_first', 'https://www.vinted.co.uk/catalog?search_text=fold&brand_ids%5B%5D=417030&currency=GBP&order=newest_first']


@dataclass
class InputField:
    name: str
    value: str = ""
    input_type: str = "text"
    placeholder: str = ""
    required: bool = False


@dataclass
class FormInfo:
    action: str
    method: str
    inputs: list[InputField]


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[FormInfo] = []
        self._current: FormInfo | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {k: (v or "") for k, v in attrs}

        if tag.lower() == "form":
            self._current = FormInfo(
                action=attrs_dict.get("action", ""),
                method=attrs_dict.get("method", "get").lower(),
                inputs=[],
            )
            return

        if tag.lower() == "input" and self._current is not None:
            name = attrs_dict.get("name", "")
            if not name:
                return
            self._current.inputs.append(
                InputField(
                    name=name,
                    value=attrs_dict.get("value", ""),
                    input_type=attrs_dict.get("type", "text").lower(),
                    placeholder=attrs_dict.get("placeholder", ""),
                    required="required" in attrs_dict,
                )
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def choose_query_form(forms: list[FormInfo]) -> tuple[FormInfo, InputField]:
    candidates: list[tuple[int, FormInfo, InputField]] = []

    for form in forms:
        for field in form.inputs:
            searchable = f"{field.name} {field.placeholder}".lower()
            score = 0
            if field.input_type in {"url", "text", "search"}:
                score += 2
            if "vinted" in searchable:
                score += 8
            if "url" in searchable or "query" in searchable or "search" in searchable:
                score += 4
            if field.required:
                score += 1
            if form.method == "post":
                score += 2
            if score:
                candidates.append((score, form, field))

    if not candidates:
        raise RuntimeError(
            "Could not identify the Add New Query form automatically."
        )

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, form, field = candidates[0]
    return form, field


def extract_existing_urls(page_html: str) -> set[str]:
    decoded = html.unescape(page_html)
    found = re.findall(
        r"https://www\.vinted\.co\.uk/catalog\?[^\"'<>\s]+",
        decoded,
    )
    return {url.rstrip("&") for url in found}


def main() -> int:
    print("Vinted Notifications bulk query importer")
    print("=" * 42)
    print(f"Queries prepared: {len(QUERIES)}")
    print()

    base = input(
        "Web address [press Enter for http://127.0.0.1:8000]: "
    ).strip() or "http://127.0.0.1:8000"
    base = base.rstrip("/")

    cookies = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies)
    )

    queries_page = f"{base}/queries"

    try:
        with opener.open(queries_page, timeout=15) as response:
            page_bytes = response.read()
            page_html = page_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        print()
        print(f"Could not open {queries_page}")
        print("Make sure Vinted Notifications is running.")
        print(f"Technical detail: {exc}")
        return 1

    parser = FormParser()
    parser.feed(page_html)

    try:
        form, url_field = choose_query_form(parser.forms)
    except RuntimeError as exc:
        print(str(exc))
        print(
            "Open the Add New Query page in your browser and check that "
            "it is available at /queries."
        )
        return 1

    action_url = urllib.parse.urljoin(
        queries_page,
        form.action or queries_page,
    )
    method = form.method.upper()

    if method != "POST":
        print(
            f"The detected form uses {method}, not POST. "
            "Stopping to avoid submitting incorrectly."
        )
        return 1

    hidden_values = {
        field.name: field.value
        for field in form.inputs
        if field.input_type == "hidden"
    }

    existing = extract_existing_urls(page_html)

    print()
    print(f"Detected form action: {action_url}")
    print(f"Detected URL field: {url_field.name}")
    print(f"Exact URLs already visible on the page: {len(existing)}")
    print()
    answer = input(
        f"Add up to {len(QUERIES)} queries now? Type YES to continue: "
    ).strip()

    if answer != "YES":
        print("Nothing was changed.")
        return 0

    added = 0
    skipped = 0
    failed = 0

    for number, query_url in enumerate(QUERIES, start=1):
        clean_url = query_url.rstrip("&")

        if clean_url in existing:
            skipped += 1
            print(f"[{number}/{len(QUERIES)}] SKIP: already present")
            continue

        payload = dict(hidden_values)
        payload[url_field.name] = clean_url
        body = urllib.parse.urlencode(payload).encode("utf-8")

        request = urllib.request.Request(
            action_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": queries_page,
                "User-Agent": "VintedNotificationsBulkImporter/1.0",
            },
        )

        try:
            with opener.open(request, timeout=20) as response:
                response.read()
            added += 1
            print(f"[{number}/{len(QUERIES)}] ADDED")
        except urllib.error.HTTPError as exc:
            failed += 1
            detail = exc.read().decode("utf-8", errors="replace")[:250]
            print(
                f"[{number}/{len(QUERIES)}] FAILED: HTTP {exc.code} "
                f"{detail!r}"
            )
        except Exception as exc:
            failed += 1
            print(f"[{number}/{len(QUERIES)}] FAILED: {exc}")

        # Be gentle with the local application.
        time.sleep(0.25)

    print()
    print("Finished")
    print(f"Added:   {added}")
    print(f"Skipped: {skipped}")
    print(f"Failed:  {failed}")
    print()
    print(f"Check the Queries page: {queries_page}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

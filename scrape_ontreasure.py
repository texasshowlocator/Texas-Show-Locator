import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# TEXAS SHOW LOCATOR - ONTREASURE SCRAPER
# Independent from the TCDB scraper and its files.
# ============================================================

BASE_URL = "https://www.ontreasure.com"
LISTING_PAGE_URL = f"{BASE_URL}/events?states=TX"
API_URL = f"{BASE_URL}/api/web/events/listing"

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

SNAPSHOT_FILE = DATA_DIR / "ontreasure_previous_snapshot.csv"
MAIN_CSV = OUTPUT_DIR / "ontreasure_events.csv"
CHANGES_CSV = OUTPUT_DIR / "ontreasure_daily_changes.csv"
AUDIT_FILE = OUTPUT_DIR / "ontreasure_scrape_audit.txt"

COLUMNS = ["Start", "End", "Event", "Venue/Address"]
CHANGE_COLUMNS = ["Change"] + COLUMNS

REQUEST_TIMEOUT = 45
REQUEST_DELAY = 0.25
MAX_RETRIES = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Referer": LISTING_PAGE_URL,
}


# ============================================================
# SESSION
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)

    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%m/%d/%y")
    except ValueError:
        return None


def format_date(value):
    parsed = parse_iso_date(value)
    if parsed:
        return parsed

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(str(value), fmt).strftime("%m/%d/%y")
        except (ValueError, TypeError):
            pass
    return None


def event_key(event):
    return (
        clean_text(event["Start"]).casefold(),
        clean_text(event["Event"]).casefold(),
        clean_text(event["Venue/Address"]).casefold(),
    )


def date_value(value):
    return datetime.strptime(value, "%m/%d/%y")


# ============================================================
# FETCH ALL LISTING PAGES FROM THE DISCOVERED ENDPOINT
# ============================================================

def get_expected_count(session):
    response = session.get(LISTING_PAGE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    title = BeautifulSoup(response.text, "html.parser").title
    title_text = clean_text(title.get_text()) if title else ""

    match = re.search(r"(\d[\d,]*)\s+Upcoming Shows", title_text, re.I)
    if match:
        return int(match.group(1).replace(",", "")), title_text

    # The title count is a completeness check, not a source of event data.
    return None, title_text


def fetch_listing_pages(session):
    all_events = []
    seen_ids = set()
    page_number = 1

    while True:
        params = {"states": "TX", "page": page_number}
        response = session.get(
            API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        try:
            page_events = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Listing API page {page_number} did not return valid JSON."
            ) from exc

        if not isinstance(page_events, list):
            raise RuntimeError(
                f"Unexpected API response on page {page_number}: "
                f"{type(page_events).__name__}"
            )

        if not page_events:
            break

        added_this_page = 0

        for item in page_events:
            if not isinstance(item, dict):
                continue

            event_id = str(item.get("id") or "").strip()
            if not event_id:
                raise RuntimeError(
                    f"An event on API page {page_number} has no ID."
                )

            if event_id in seen_ids:
                continue

            seen_ids.add(event_id)
            all_events.append(item)
            added_this_page += 1

        print(
            f"API page {page_number}: {len(page_events)} returned, "
            f"{added_this_page} new events"
        )

        # Avoid an endless loop if the endpoint repeats a page.
        if added_this_page == 0:
            raise RuntimeError(
                f"API pagination repeated data on page {page_number}."
            )

        if len(page_events) < 12:
            break

        page_number += 1
        time.sleep(0.15)

    if not all_events:
        raise RuntimeError("The OnTreasure API returned no Texas events.")

    return all_events, page_number


# ============================================================
# EVENT DETAIL PAGE / JSON-LD EXTRACTION
# ============================================================

def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_event_jsonld(soup):
    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for obj in walk_json(payload):
            event_type = obj.get("@type", "")
            types = event_type if isinstance(event_type, list) else [event_type]

            if not any("event" in str(item).casefold() for item in types):
                continue

            location = obj.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            if not isinstance(location, dict):
                location = {}

            address = location.get("address") or {}
            if isinstance(address, str):
                address_text = address
                address = {}
            elif isinstance(address, dict):
                address_text = ""
            else:
                address_text = ""
                address = {}

            return {
                "name": clean_text(obj.get("name")),
                "start": obj.get("startDate"),
                "end": obj.get("endDate"),
                "venue": clean_text(location.get("name")),
                "street": clean_text(address.get("streetAddress")),
                "city": clean_text(address.get("addressLocality")),
                "state": clean_text(address.get("addressRegion")),
                "postal": clean_text(address.get("postalCode")),
                "address_text": clean_text(address_text),
            }

    return {}


def extract_address_from_visible_text(soup):
    lines = [
        clean_text(line)
        for line in soup.get_text("\n", strip=True).splitlines()
        if clean_text(line)
    ]

    # Treasure often renders the full venue and address on one line,
    # ending in "TX, USA" without a ZIP code.
    for line in lines:
        if re.search(r",\s*(?:TX|Texas)(?:\s+\d{5}(?:-\d{4})?)?\s*,?\s*USA\b", line, re.I):
            return line

    # Some pages show the address as separate lines under "Event Location".
    for index, line in enumerate(lines):
        if "event location" in line.casefold():
            section = []
            remainder = re.sub(r"^.*?event location\s*:?", "", line, flags=re.I).strip()
            if remainder:
                section.append(remainder)

            for candidate in lines[index + 1:index + 5]:
                if candidate.casefold() in {"map unavailable on this device", "open the location in your maps app instead"}:
                    break
                section.append(candidate)
                if re.search(r"\b(?:TX|Texas)\b", candidate, re.I):
                    break

            location_text = ", ".join(section)
            if re.search(r"\b(?:TX|Texas)\b", location_text, re.I):
                return location_text

    # Last fallback: collect a street line and its following Texas city line.
    for index, line in enumerate(lines):
        if re.search(r",\s*(?:TX|Texas)\s+\d{5}\b", line, re.I):
            return ", ".join(lines[max(0, index - 2):index + 1])

    return ""


def fetch_event_details(session, item):
    slug = clean_text(item.get("cleaned_name"))
    if not slug:
        raise ValueError(f"Event has no detail-page slug: {item.get('name')}")

    detail_url = urljoin(BASE_URL, f"/events/{slug}")
    response = session.get(detail_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    structured = extract_event_jsonld(soup)

    name = (
        structured.get("name")
        or clean_text(item.get("name"))
    )

    start = (
        format_date(structured.get("start"))
        or format_date(item.get("min_date"))
    )
    end = (
        format_date(structured.get("end"))
        or format_date(item.get("max_date"))
        or start
    )

    if not start or not end:
        raise ValueError(f"Missing event dates for {name}")

    api_state = clean_text(item.get("state")).upper()
    detail_state = clean_text(structured.get("state")).upper()

    if api_state != "TX":
        raise ValueError(f"API returned a non-Texas event: {name} ({api_state})")

    if detail_state and detail_state not in ("TX", "TEXAS"):
        raise ValueError(
            f"Detail page identifies a non-Texas event: {name} ({detail_state})"
        )

    city = (
        structured.get("city")
        or clean_text(item.get("city"))
    )
    venue = structured.get("venue", "")
    street = structured.get("street", "")
    postal = structured.get("postal", "")
    address_text = structured.get("address_text", "")

    # Use rendered page content to fill missing venue/address fields.
    # The structured data may contain a venue name but omit its street address.
    if not street or not venue or not address_text:
        fallback = extract_address_from_visible_text(soup)
        if fallback:
            # If the rendered location already includes the structured venue
            # or street, keep the complete line instead of duplicating it.
            if venue and venue.casefold() in fallback.casefold():
                venue = ""
            if street and street.casefold() in fallback.casefold():
                street = ""
            if fallback.casefold() != address_text.casefold():
                address_text = fallback

    address_parts = []

    if venue:
        address_parts.append(venue)

    if street and street.casefold() not in {
        part.casefold() for part in address_parts
    }:
        address_parts.append(street)

    if address_text and address_text.casefold() not in {
        part.casefold() for part in address_parts
    }:
        address_parts.append(address_text)

    # The API always provides the Texas city. Use it when the detail
    # page does not expose a venue or street address.
    if city:
        city_line = f"{city}, TX"
        if postal:
            city_line += f" {postal}"

        if not any(city.casefold() in part.casefold() for part in address_parts):
            address_parts.append(city_line)

    venue_address = ", ".join(
        part for part in address_parts if part
    )

    return {
        "Start": start,
        "End": end,
        "Event": name,
        "Venue/Address": venue_address,
    }


# ============================================================
# MERGE CONSECUTIVE DATES FOR SAME EVENT AND VENUE
# ============================================================

def merge_consecutive_events(events):
    grouped = {}

    for event in events:
        key = (
            clean_text(event["Event"]).casefold(),
            clean_text(event["Venue/Address"]).casefold(),
        )
        grouped.setdefault(key, []).append(event.copy())

    merged = []

    for group in grouped.values():
        group.sort(key=lambda row: date_value(row["Start"]))
        current = None

        for event in group:
            event_start = date_value(event["Start"])
            event_end = date_value(event["End"])

            if current is None:
                current = event.copy()
                continue

            current_end = date_value(current["End"])

            if event_start <= current_end + timedelta(days=1):
                if event_end > current_end:
                    current["End"] = event_end.strftime("%m/%d/%y")
            else:
                merged.append(current)
                current = event.copy()

        if current is not None:
            merged.append(current)

    return merged


# ============================================================
# CSV AND SNAPSHOT HELPERS
# ============================================================

def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def read_snapshot():
    if not SNAPSHOT_FILE.exists():
        return [], False

    with SNAPSHOT_FILE.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        reader = csv.DictReader(file)

        if reader.fieldnames != COLUMNS:
            return [], False

        return [
            {column: row.get(column, "") for column in COLUMNS}
            for row in reader
        ], True


def update_snapshot(events):
    temp_file = SNAPSHOT_FILE.with_suffix(".tmp")
    write_csv(temp_file, events, COLUMNS)
    temp_file.replace(SNAPSHOT_FILE)


def calculate_changes(current, previous):
    current_map = {event_key(row): row for row in current}
    previous_map = {event_key(row): row for row in previous}
    changes = []

    for key in current_map.keys() - previous_map.keys():
        row = current_map[key].copy()
        row["Change"] = "NEW"
        changes.append(row)

    for key in previous_map.keys() - current_map.keys():
        row = previous_map[key].copy()
        row["Change"] = "REMOVED"
        changes.append(row)

    return sorted(
        changes,
        key=lambda row: (
            row["Start"],
            row["Event"].casefold(),
            row["Change"],
        ),
    )


# ============================================================
# AUDIT REPORT
# ============================================================

def write_audit(
    status,
    expected=None,
    listing_count=0,
    pages=0,
    detail_failures=None,
    new_count=0,
    removed_count=0,
    error=None,
    baseline=False,
):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with AUDIT_FILE.open("w", encoding="utf-8") as file:
        file.write("TEXAS SHOW LOCATOR - ONTREASURE SCRAPE AUDIT\n")
        file.write("=" * 55 + "\n")
        file.write(
            f"Run time: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        file.write(f"Source: {LISTING_PAGE_URL}\n")
        file.write(f"Status: {status}\n")
        file.write(f"Website-reported event count: {expected}\n")
        file.write(f"Unique API events collected: {listing_count}\n")
        file.write(f"API pages read: {pages}\n")
        file.write(f"New events: {new_count}\n")
        file.write(f"Removed events: {removed_count}\n")

        if baseline:
            file.write(
                "\nA new snapshot baseline was created. "
                "No changes were reported for this run.\n"
            )

        if error:
            file.write(f"\nERROR:\n{error}\n")

        if detail_failures:
            file.write("\nEVENT DETAIL PAGES THAT FAILED:\n")
            for failure in detail_failures:
                file.write(f"- {failure}\n")


# ============================================================
# MAIN SCRAPER
# ============================================================

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("TEXAS SHOW LOCATOR - ONTREASURE SCRAPER")
    print("=" * 60)

    session = create_session()

    try:
        expected_count, page_title = get_expected_count(session)
        print(f"Website title: {page_title}")
        print(f"Website-reported count: {expected_count}")

        listing_items, page_count = fetch_listing_pages(session)
        print(f"Unique Texas API events: {len(listing_items)}")

        # Fail closed if pagination did not return the count advertised
        # by the listing page. Never update the snapshot on an incomplete run.
        if expected_count is not None and len(listing_items) != expected_count:
            raise RuntimeError(
                f"Incomplete listing: website reports {expected_count} "
                f"events, API returned {len(listing_items)}."
            )

        events = []
        failures = []

        for index, item in enumerate(listing_items, start=1):
            label = clean_text(item.get("name")) or item.get("id", "Unknown")
            print(f"Detail {index}/{len(listing_items)}: {label}")

            try:
                events.append(fetch_event_details(session, item))
            except Exception as exc:
                failures.append(f"{label}: {exc}")

            time.sleep(REQUEST_DELAY)

        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(listing_items)} event detail pages "
                "could not be processed. Snapshot and reports were preserved."
            )

        if len(events) != len(listing_items):
            raise RuntimeError(
                f"Event count mismatch: {len(events)} detail records for "
                f"{len(listing_items)} API listings."
            )

        # Combine adjacent dates for events with matching names and venues.
        events = merge_consecutive_events(events)
        events.sort(
            key=lambda row: (
                date_value(row["Start"]),
                row["Event"].casefold(),
            )
        )

        previous, compatible = read_snapshot()

        if compatible:
            changes = calculate_changes(events, previous)
            baseline = False
        else:
            changes = []
            baseline = True

        new_count = sum(row["Change"] == "NEW" for row in changes)
        removed_count = sum(row["Change"] == "REMOVED" for row in changes)

        write_csv(MAIN_CSV, events, COLUMNS)
        write_csv(CHANGES_CSV, changes, CHANGE_COLUMNS)
        update_snapshot(events)

        write_audit(
            status="SUCCESS",
            expected=expected_count,
            listing_count=len(listing_items),
            pages=page_count,
            new_count=new_count,
            removed_count=removed_count,
            baseline=baseline,
        )

        print("\n" + "=" * 60)
        print("ONTREASURE SCRAPE COMPLETED SUCCESSFULLY")
        print("=" * 60)
        print(f"Website-reported events: {expected_count}")
        print(f"API events processed: {len(listing_items)}")
        print(f"API pages read: {page_count}")
        print(f"Output rows after date merging: {len(events)}")
        print(f"New events: {new_count}")
        print(f"Removed events: {removed_count}")
        if baseline:
            print("Snapshot: NEW BASELINE CREATED")

        print("\nFILES CREATED:")
        print(f"Master CSV: {MAIN_CSV}")
        print(f"Daily changes: {CHANGES_CSV}")
        print(f"Audit report: {AUDIT_FILE}")
        print(f"Snapshot: {SNAPSHOT_FILE}")

    except Exception as exc:
        message = str(exc)
        print(f"\nONTREASURE SCRAPE FAILED: {message}")
        print("Previous snapshot and reports were not updated.")

        write_audit(
            status="FAILED - SNAPSHOT PRESERVED",
            error=message,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ============================================================
# TEXAS SHOW LOCATOR - TCG SHOWS NEAR ME SCRAPER
# Independent from TCDB and OnTreasure files and snapshots.
# ============================================================

BASE_URL = "https://tcgshowsnearme.com"
LISTING_URL = f"{BASE_URL}/events/pillar/usa/texas"

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

SNAPSHOT_FILE = DATA_DIR / "tcgshowsnearme_previous_snapshot.csv"
MAIN_CSV = OUTPUT_DIR / "tcgshowsnearme_events.csv"
CHANGES_CSV = OUTPUT_DIR / "tcgshowsnearme_daily_changes.csv"
AUDIT_FILE = OUTPUT_DIR / "tcgshowsnearme_scrape_audit.txt"

COLUMNS = ["Start", "End", "Event", "Venue/Address"]
CHANGE_COLUMNS = ["Change"] + COLUMNS

REQUEST_TIMEOUT = 90000
PAGE_DELAY = 0.5
DETAIL_DELAY = 0.25
MAX_LISTING_PAGES = 100

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_date(value):
    if not value:
        return None

    value = clean_text(value)

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).strftime("%m/%d/%y")
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%A, %B %d, %Y",
        "%a, %b %d, %Y",
    ):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%y")
        except ValueError:
            continue

    return None


def date_value(value):
    return datetime.strptime(value, "%m/%d/%y")


def event_key(event):
    return (
        clean_text(event["Start"]).casefold(),
        clean_text(event["Event"]).casefold(),
        clean_text(event["Venue/Address"]).casefold(),
    )


def normalize_state(value):
    value = clean_text(value).casefold()
    return "TX" if value in ("tx", "texas") else value.upper()


def make_page_url(page_number):
    parsed = urlparse(LISTING_URL)
    query = parse_qs(parsed.query)
    if page_number == 1:
        query.pop("page", None)
    else:
        query["page"] = [str(page_number)]
    return urlunparse(
        parsed._replace(query=urlencode(query, doseq=True))
    )


# ============================================================
# LISTING PAGE DISCOVERY AND PAGINATION
# ============================================================

def extract_expected_count(soup):
    text = clean_text(soup.get_text(" ", strip=True))
    patterns = (
        r"([\d,]+)\s+Card Shows in Texas",
        r"([\d,]+)\s+(?:Upcoming )?Events? in Texas",
        r"([\d,]+)\s+upcoming",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def is_event_detail_url(href):
    path = urlparse(href).path.strip("/")
    if not path.startswith("events/"):
        return False

    excluded = (
        "events/pillar/",
        "events/d/",
        "events/in/",
        "events/organizer",
        "events/category",
    )
    return not any(path.startswith(item) for item in excluded)


def collect_listing_links(page):
    soup = BeautifulSoup(page.content(), "html.parser")
    links = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        if not is_event_detail_url(href):
            continue

        url = urljoin(BASE_URL, href.split("#", 1)[0])
        if url in seen:
            continue

        seen.add(url)
        links.append({
            "url": url,
            "name": clean_text(anchor.get_text(" ", strip=True)),
        })

    return links, soup


def fetch_all_listing_links(page):
    all_links = []
    seen_urls = set()
    expected_count = None
    pages_read = 0
    empty_pages = 0

    for page_number in range(1, MAX_LISTING_PAGES + 1):
        url = make_page_url(page_number)
        print(f"Loading Texas listing page {page_number}: {url}")

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT,
        )
        if response and response.status >= 400:
            raise RuntimeError(
                f"Listing page {page_number} returned HTTP {response.status}"
            )

        page.locator("body").wait_for(timeout=20000)
        links, soup = collect_listing_links(page)
        pages_read += 1

        if page_number == 1:
            expected_count = extract_expected_count(soup)

        added = 0
        for item in links:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_links.append(item)
                added += 1

        print(
            f"Page {page_number}: {len(links)} event links, "
            f"{added} new, {len(all_links)} total"
        )

        if expected_count is not None and len(all_links) >= expected_count:
            break

        if added == 0:
            empty_pages += 1
            if empty_pages >= 1:
                break
        else:
            empty_pages = 0

        # The site may use ordinary page-number pagination without exposing
        # a crawlable next link. Try the next page until it returns no new cards.
        time.sleep(PAGE_DELAY)

    if not all_links:
        raise RuntimeError(
            "No Texas event detail links were found. "
            "The website layout or listing URL may have changed."
        )

    if expected_count is not None and len(all_links) != expected_count:
        raise RuntimeError(
            f"Incomplete Texas listing: page reports {expected_count} events, "
            f"but pagination returned {len(all_links)} unique event links "
            f"across {pages_read} pages. Snapshot was not changed."
        )

    return all_links, expected_count, pages_read


# ============================================================
# JSON-LD AND DETAIL PAGE EXTRACTION
# ============================================================

def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_jsonld_event(soup):
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


def extract_dates_from_text(text):
    text = clean_text(text)
    month = (
        r"(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)"
    )

    # Full date or date range where the second date includes a month.
    full_range = re.search(
        rf"({month}\s+\d{{1,2}},?\s+\d{{4}})\s*[-–—to]+\s*"
        rf"({month}\s+\d{{1,2}},?\s+\d{{4}})",
        text,
        re.IGNORECASE,
    )
    if full_range:
        return parse_date(full_range.group(1)), parse_date(full_range.group(2))

    # Detail pages commonly show one full date in the page heading.
    single = re.search(
        rf"({month}\s+\d{{1,2}},?\s+\d{{4}})",
        text,
        re.IGNORECASE,
    )
    if single:
        parsed = parse_date(single.group(1))
        return parsed, parsed

    return None, None


def extract_location_from_text(soup, rendered_text):
    # Prefer the visible "Getting there" section on the detail page.
    lines = [
        clean_text(line)
        for line in rendered_text.splitlines()
        if clean_text(line)
    ]

    for index, line in enumerate(lines):
        if line.casefold() == "getting there":
            section = lines[index + 1:index + 7]
            useful = []
            for candidate in section:
                if candidate.casefold() in {
                    "directions", "event details", "ticket information",
                    "united states",
                }:
                    if candidate.casefold() == "united states":
                        break
                    continue
                useful.append(candidate)
                if re.search(r"\b(?:TX|Texas)\b", candidate, re.I):
                    break
            if useful and any(
                re.search(r"\b(?:TX|Texas)\b", item, re.I)
                for item in useful
            ):
                return ", ".join(useful)

    # Last fallback: find an address line containing a Texas state marker.
    for index, line in enumerate(lines):
        if re.search(r"\b(?:TX|Texas)\b", line, re.I):
            return ", ".join(lines[max(0, index - 2):index + 1])

    return ""


def fetch_event_detail(page, listing):
    response = page.goto(
        listing["url"],
        wait_until="domcontentloaded",
        timeout=REQUEST_TIMEOUT,
    )
    if response and response.status >= 400:
        raise RuntimeError(f"Detail page returned HTTP {response.status}")

    page.locator("body").wait_for(timeout=20000)
    rendered_text = page.locator("body").inner_text(timeout=20000)
    soup = BeautifulSoup(page.content(), "html.parser")

    structured = extract_jsonld_event(soup)
    name = structured.get("name") or listing["name"]

    start = parse_date(structured.get("start"))
    end = parse_date(structured.get("end"))

    if not start:
        start, text_end = extract_dates_from_text(rendered_text)
        end = end or text_end

    if not start:
        raise ValueError(f"Could not determine event start date for {name}")

    if not end:
        end = start

    state = normalize_state(structured.get("state"))
    if state and state != "TX":
        raise ValueError(f"Detail page is not in Texas: {name} ({state})")

    venue = structured.get("venue", "")
    street = structured.get("street", "")
    city = structured.get("city", "")
    postal = structured.get("postal", "")
    address_text = structured.get("address_text", "")

    if not address_text or not city:
        fallback = extract_location_from_text(soup, rendered_text)
        if fallback:
            address_text = fallback

    address_parts = []
    for part in (venue, street, address_text):
        part = clean_text(part)
        if part and part.casefold() not in {
            existing.casefold() for existing in address_parts
        }:
            address_parts.append(part)

    if city:
        city_line = f"{city}, TX"
        if postal:
            city_line += f" {postal}"
        if not any(city.casefold() in part.casefold() for part in address_parts):
            address_parts.append(city_line)

    venue_address = ", ".join(address_parts)

    # Every event must be verifiably Texas. The Texas pillar listing is the
    # primary filter; this check catches contradictory detail-page data.
    combined_location = " ".join(
        [venue_address, rendered_text[:2500]]
    )
    if state != "TX" and not re.search(
        r"\bTX\b|\bTexas\b", combined_location, re.IGNORECASE
    ):
        raise ValueError(
            f"Could not verify Texas location for {name}; "
            "event skipped and the run will be marked incomplete."
        )

    return {
        "Start": start,
        "End": end,
        "Event": name,
        "Venue/Address": venue_address,
    }


# ============================================================
# MERGE CONSECUTIVE DATES FOR MATCHING EVENTS AND VENUES
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
# CSV, SNAPSHOT, AND CHANGE REPORTS
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
            date_value(row["Start"]),
            row["Event"].casefold(),
            row["Change"],
        ),
    )


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
        file.write("TEXAS SHOW LOCATOR - TCG SHOWS NEAR ME SCRAPE AUDIT\n")
        file.write("=" * 62 + "\n")
        file.write(f"Run time: {datetime.now().isoformat(timespec='seconds')}\n")
        file.write(f"Source: {LISTING_URL}\n")
        file.write(f"Status: {status}\n")
        file.write(f"Website-reported event count: {expected}\n")
        file.write(f"Unique Texas event links collected: {listing_count}\n")
        file.write(f"Listing pages read: {pages}\n")
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
# MAIN
# ============================================================

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 65)
    print("TEXAS SHOW LOCATOR - TCG SHOWS NEAR ME SCRAPER")
    print("=" * 65)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 1100},
            )
            page = context.new_page()

            try:
                listings, expected_count, pages_read = fetch_all_listing_links(page)
                print(f"Unique event links collected: {len(listings)}")

                events = []
                failures = []

                for index, listing in enumerate(listings, start=1):
                    label = listing["name"] or listing["url"]
                    print(
                        f"Detail {index}/{len(listings)}: {label}"
                    )
                    try:
                        events.append(fetch_event_detail(page, listing))
                    except Exception as exc:
                        failures.append(f"{label} | {listing['url']} | {exc}")
                        print(f"  FAILED: {exc}")
                    time.sleep(DETAIL_DELAY)
            finally:
                browser.close()

        if failures:
            raise RuntimeError(
                f"{len(failures)} of {len(listings)} event detail pages failed. "
                "The master report and previous snapshot were preserved."
            )

        if len(events) != len(listings):
            raise RuntimeError(
                f"Event count mismatch: {len(events)} detail records for "
                f"{len(listings)} listing links."
            )

        if expected_count is not None and len(listings) != expected_count:
            raise RuntimeError(
                f"Expected {expected_count} events but collected {len(listings)}."
            )

        # Remove duplicate event rows before merging date ranges.
        unique = {}
        for event in events:
            unique[event_key(event)] = event
        events = list(unique.values())

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
            listing_count=len(listings),
            pages=pages_read,
            new_count=new_count,
            removed_count=removed_count,
            baseline=baseline,
        )

        print("\n" + "=" * 65)
        print("TCG SHOWS NEAR ME SCRAPE COMPLETED SUCCESSFULLY")
        print("=" * 65)
        print(f"Website-reported events: {expected_count}")
        print(f"Event detail pages processed: {len(events)}")
        print(f"Rows after date merging: {len(events)}")
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
        print(f"\nTCG SHOWS NEAR ME SCRAPE FAILED: {message}")
        print("Previous snapshot and reports were not updated.")
        write_audit(
            status="FAILED - SNAPSHOT PRESERVED",
            error=message,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

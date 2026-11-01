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
LISTING_URL = f"{BASE_URL}/events/in/usa/texas"

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
        r"([\d,]+)\s+(?:confirmed\s+)?Card Shows in Texas",
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
    """
    Use the Texas-wide upcoming-events listing. Unlike the pillar page,
    this route presents a single statewide list and does not require visiting
    city pages, which have returned HTTP 403 in GitHub Actions.

    The headline count is informational: it may count event occurrences,
    while this scraper deduplicates event detail URLs.
    """
    print(f"Loading statewide Texas event page: {LISTING_URL}")
    response = page.goto(
        LISTING_URL,
        wait_until="domcontentloaded",
        timeout=REQUEST_TIMEOUT,
    )
    if response and response.status >= 400:
        raise RuntimeError(
            f"Texas statewide listing returned HTTP {response.status}"
        )

    page.locator("body").wait_for(timeout=20000)
    links, soup = collect_listing_links(page)
    expected_count = extract_expected_count(soup)

    if not links:
        raise RuntimeError(
            "No Texas event detail links were found on the statewide listing. "
            "The page may be blocking automated access or its layout may have changed."
        )

    print(
        f"Collected {len(links)} unique event detail links from "
        "the statewide Texas listing."
    )
    return links, expected_count, 1

# ============================================================
# DIRECT EXTRACTION FROM THE STATEWIDE LISTING
# ============================================================

def is_event_detail_url(href):
    path = urlparse(href).path.strip("/")
    if not path.startswith("events/"):
        return False
    excluded = (
        "events/pillar/", "events/d/", "events/in/",
        "events/organizer", "events/category", "events/seasonal/",
    )
    return not any(path.startswith(item) for item in excluded)


def debug_card_structure(anchor):
    """Describe the actual listing DOM around a link when card detection fails."""
    details = []
    details.append(
        "ANCHOR TEXT: " + clean_text(anchor.get_text(" ", strip=True))[:900]
    )
    node = anchor
    for level in range(35):
        if node is None:
            break
        if getattr(node, "name", None):
            classes = node.get("class", [])
            if isinstance(classes, list):
                classes = " ".join(classes)
            node_text = clean_text(node.get_text(" ", strip=True))
            details.append(
                f"PARENT {level}: <{node.name}> "
                f"class={str(classes)[:180]!r} id={str(node.get('id', ''))[:100]!r} "
                f"text_len={len(node_text)} text={node_text[:650]!r}"
            )
        node = node.parent
    return " || ".join(details)


def parse_listing_dates(text):
    """Parse a single date or a weekday/month date range from a listing card."""
    month_pattern = (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)"
    )
    date_pattern = re.compile(
        rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
        rf"(?P<month>{month_pattern})\s+(?P<day>\d{{1,2}})"
        rf"(?:,?\s+(?P<year>\d{{4}}))?",
        re.IGNORECASE,
    )
    matches = list(date_pattern.finditer(text))
    if not matches:
        return None, None, None

    first = matches[0]
    second = None
    if len(matches) > 1:
        between = text[first.end():matches[1].start()]
        if re.search(r"[-–—]|\bto\b", between, re.IGNORECASE):
            second = matches[1]

    def make_date(match, default_year=None):
        year = int(match.group("year")) if match.group("year") else default_year
        if year is None:
            return None
        month_name = match.group("month")[:3].title()
        month_number = datetime.strptime(month_name, "%b").month
        return datetime(year, month_number, int(match.group("day")))

    if second:
        end_year = int(second.group("year")) if second.group("year") else (
            int(first.group("year")) if first.group("year") else None
        )
        start_year = int(first.group("year")) if first.group("year") else end_year
        if start_year is None:
            return None, None, first
        start_date = make_date(first, start_year)
        end_date = make_date(second, end_year)
        if end_date and start_date and end_date < start_date and not first.group("year"):
            start_date = make_date(first, end_year - 1)
        return start_date, end_date, first

    year = int(first.group("year")) if first.group("year") else None
    if year is None:
        return None, None, first
    single = make_date(first, year)
    return single, single, first


def extract_listing_event(anchor):
    """
    Extract an event from the nearest card-like ancestor containing its date,
    time and Texas location. The live Texas page uses both multi-line cards
    and compact one-line cards, so parsing only the anchor text is insufficient.
    """
    href = clean_text(anchor.get("href"))
    node = anchor
    card_text = ""
    for _ in range(10):
        if node is None or not getattr(node, "name", None):
            break
        candidate = node.get_text(" ", strip=True)
        candidate = clean_text(candidate)
        has_date = re.search(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
            candidate,
            re.IGNORECASE,
        )
        has_time = re.search(
            r"\d{1,2}:\d{2}\s*(?:AM|PM)?\s*[-–]\s*\d{1,2}:\d{2}",
            candidate,
            re.IGNORECASE,
        )
        has_texas = re.search(r"\b(?:TX|Texas)\b", candidate, re.IGNORECASE)
        if has_date and has_time and has_texas and len(candidate) <= 3500:
            card_text = candidate
            break
        node = node.parent

    if not card_text:
        raise ValueError("Could not find a card containing date, time and Texas location")

    start_date, end_date, date_match = parse_listing_dates(card_text)
    if not start_date or not end_date:
        raise ValueError("Could not parse complete event date or date range")

    date_start = date_match.start()
    before_date = card_text[:date_start].strip()
    name = re.split(r"\s+Hosted by\s+", before_date, maxsplit=1, flags=re.I)[0]
    name = clean_text(name)
    name = re.sub(r"^(?:Featured\s+)+", "", name, flags=re.I).strip()
    if not name:
        # Some cards put the event name after the date; fall back to the
        # anchor's visible text before the host label.
        anchor_text = clean_text(anchor.get_text(" ", strip=True))
        name = re.split(r"\s+Hosted by\s+", anchor_text, maxsplit=1, flags=re.I)[0]
    if not name:
        raise ValueError("Could not determine event name")

    time_match = re.search(
        r"\d{1,2}:\d{2}\s*(?:AM|PM)?\s*[-–]\s*"
        r"\d{1,2}:\d{2}\s*(?:AM|PM)?",
        card_text[date_match.end():],
        re.IGNORECASE,
    )
    if not time_match:
        raise ValueError(f"Could not locate event time for {name}")

    location_text = card_text[date_match.end() + time_match.end():].strip()
    feature_re = re.compile(
        r"\s+(?:Trade Night|"
        r"\d+\+?\s+Vendors?|"
        r"Free Entry|Free Parking|Free for Kids|Free Admission(?: for Kids)?|"
        r"Prize Draw|Giveaway|Tournament|Tournaments|"
        r"Early Bird|Door Prizes|Admission|Entry|"
        r"\$\s*\d+(?:\.\d{2})?).*$",
        re.IGNORECASE,
    )
    location_text = feature_re.sub("", location_text).strip(" ,")

    if not re.search(r"\b(?:TX|Texas)\b", location_text, re.IGNORECASE):
        raise ValueError(f"Could not verify Texas location for {name}")
    if not location_text:
        raise ValueError(f"Could not extract venue/address for {name}")

    return {
        "Start": start_date.strftime("%m/%d/%y"),
        "End": end_date.strftime("%m/%d/%y"),
        "Event": name,
        "Venue/Address": location_text,
    }

def extract_events_from_listing(page):
    soup = BeautifulSoup(page.content(), "html.parser")
    events = []
    failures = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        if not is_event_detail_url(href):
            continue
        try:
            event = extract_listing_event(anchor)
            key = event_key(event)
            if key not in seen:
                seen.add(key)
                events.append(event)
        except Exception as exc:
            failures.append(f"{href} | {exc}")

    if not events:
        detail = "; ".join(failures[:5])
        raise RuntimeError(
            "No complete event records could be extracted from the Texas listing. "
            + detail
        )

    return events, failures


def fetch_all_listing_events(page):
    """
    Fetch the statewide Texas landing page, then each Texas city page linked
    from its Browse by City section. City pages expose older and much later
    future events that are not all included in the statewide first screen.
    """
    all_events = []
    failures = []
    visited_urls = set()
    expected_count = None
    pages_read = 0

    response = page.goto(
        LISTING_URL,
        wait_until="domcontentloaded",
        timeout=REQUEST_TIMEOUT,
    )
    if response and response.status >= 400:
        raise RuntimeError(
            f"Texas statewide listing returned HTTP {response.status}"
        )
    page.locator("body").wait_for(timeout=20000)
    main_soup = BeautifulSoup(page.content(), "html.parser")
    expected_count = extract_expected_count(main_soup)

    city_urls = []
    for anchor in main_soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        absolute = urljoin(BASE_URL, href)
        parsed_path = urlparse(absolute).path.rstrip("/")
        prefix = "/events/in/usa/texas/"
        if parsed_path.startswith(prefix) and parsed_path != "/events/in/usa/texas":
            if absolute not in city_urls:
                city_urls.append(absolute)

    source_urls = [LISTING_URL] + city_urls
    print(f"Discovered {len(city_urls)} Texas city pages from the statewide listing.")

    for source_url in source_urls:
        response = page.goto(
            source_url,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT,
        )
        if response and response.status >= 400:
            raise RuntimeError(
                f"Texas source page returned HTTP {response.status}: {source_url}. "
                "Previous snapshot will be preserved."
            )
        page.locator("body").wait_for(timeout=20000)
        soup = BeautifulSoup(page.content(), "html.parser")
        pages_read += 1

        page_events = 0
        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor.get("href"))
            if not is_event_detail_url(href):
                continue
            event_url = urljoin(BASE_URL, href.split("#", 1)[0])
            if event_url in visited_urls:
                continue
            visited_urls.add(event_url)

            try:
                event = extract_listing_event(anchor)
                all_events.append(event)
                page_events += 1
            except Exception as exc:
                failures.append(f"{source_url} | {event_url} | {exc}")

        print(
            f"Read {source_url}: {page_events} event records parsed "
            f"({len(all_events)} total unique event links)."
        )
        time.sleep(PAGE_DELAY)

    if not all_events:
        detail = "; ".join(failures[:8])
        raise RuntimeError(
            "No Texas event records could be extracted from the statewide "
            "or city listings. " + detail
        )

    return all_events, failures, expected_count, pages_read


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
        file.write(f"Texas event records extracted from listing: {listing_count}\n")
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
            file.write("\nLISTING CARDS SKIPPED / EXTRACTION ERRORS:\n")
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

    listings = []
    expected_count = None
    pages_read = 0
    failures = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 1100},
            )
            page = context.new_page()

            try:
                events, failures, expected_count, pages_read = (
                    fetch_all_listing_events(page)
                )
                listings = [{"url": "", "name": row["Event"]} for row in events]
                print(
                    f"Extracted {len(events)} event records across "
                    f"{pages_read} Texas listing and city pages."
                )
                if failures:
                    print(
                        "Listing cards skipped because fields were incomplete: "
                        f"{len(failures)}"
                    )
            finally:
                browser.close()

        if len(events) < 1:
            raise RuntimeError("No Texas event records were extracted from the listing.")

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
            detail_failures=failures[:10],
            baseline=baseline,
        )

        print("\n" + "=" * 65)
        print("TCG SHOWS NEAR ME SCRAPE COMPLETED SUCCESSFULLY")
        print("=" * 65)
        print(f"Website-reported events: {expected_count}")
        print(f"Event records before date merging: {len(events)}")
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
            expected=expected_count,
            listing_count=len(listings),
            pages=pages_read,
            detail_failures=failures[:10],
            error=message,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

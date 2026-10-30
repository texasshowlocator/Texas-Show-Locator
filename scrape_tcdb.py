import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# TEXAS SHOW LOCATOR - TCDB ONLY
# CSV OUTPUT - FOUR COLUMNS (NO REGION)
# ============================================================

TCDB_URL = (
    "https://www.tcdb.com/CardShows.cfm"
    "?MODE=Location&State=TX&Display=Texas&Country=United%20States"
)

BASE_URL = "https://www.tcdb.com"

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

SNAPSHOT_FILE = DATA_DIR / "previous_snapshot.csv"
MAIN_CSV = OUTPUT_DIR / "texas_card_shows.csv"
CHANGES_CSV = OUTPUT_DIR / "daily_changes.csv"
AUDIT_FILE = OUTPUT_DIR / "scrape_audit.txt"

COLUMNS = [
    "Start",
    "End",
    "Event",
    "Venue/Address",
]

CHANGE_COLUMNS = ["Change"] + COLUMNS

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT = 45

# Increased delay to reduce the likelihood of HTTP 429 errors.
REQUEST_DELAY = 1.5

# Retry temporary server errors and rate limits.
MAX_RETRIES = 5


# ============================================================
# SESSION AND RETRY CONFIGURATION
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)

    retry_strategy = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
        raise_on_status=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy,
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


def normalize_city(city):
    city = clean_text(city)
    return city.strip(" ,")


def parse_date(value):
    """
    Convert common date formats to MM/DD/YY.
    """
    if not value:
        return None

    value = clean_text(value)

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).strftime("%m/%d/%y")
    except ValueError:
        pass

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%A, %B %d, %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                value, fmt
            ).strftime("%m/%d/%y")
        except ValueError:
            continue

    return None


def parse_date_range(text):
    """
    Find a date range in visible event-page text.
    Returns (start, end), or (None, None).
    """
    month = (
        r"(?:January|February|March|April|May|June|July|"
        r"August|September|October|November|December)"
    )

    pattern = re.compile(
        rf"({month}\s+\d{{1,2}},?\s+\d{{4}})"
        rf"\s*(?:-|–|—|to|through)\s*"
        rf"({month}\s+\d{{1,2}},?\s+\d{{4}})",
        re.IGNORECASE,
    )

    match = pattern.search(text)

    if match:
        return (
            parse_date(match.group(1)),
            parse_date(match.group(2)),
        )

    return None, None


def event_key(event):
    """
    Event comparison key:
    start date + event name + venue/address.
    """
    return (
        clean_text(event["Start"]).casefold(),
        clean_text(event["Event"]).casefold(),
        clean_text(event["Venue/Address"]).casefold(),
    )


# ============================================================
# JSON-LD EXTRACTION
# ============================================================

def walk_json(data):
    """
    Recursively find dictionaries inside JSON-LD data.
    """
    if isinstance(data, dict):
        yield data

        for value in data.values():
            yield from walk_json(value)

    elif isinstance(data, list):
        for value in data:
            yield from walk_json(value)


def get_jsonld_objects(soup):
    objects = []

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw)
            objects.extend(walk_json(data))
        except (json.JSONDecodeError, TypeError):
            continue

    return objects


def extract_jsonld_event(soup):
    """
    Return the most useful Event object and its location.
    """
    for obj in get_jsonld_objects(soup):
        event_type = obj.get("@type", "")

        if isinstance(event_type, list):
            event_types = [
                str(item).casefold()
                for item in event_type
            ]
        else:
            event_types = [str(event_type).casefold()]

        if not any("event" in item for item in event_types):
            continue

        location = obj.get("location", {})

        if isinstance(location, list):
            location = location[0] if location else {}

        if not isinstance(location, dict):
            location = {}

        address = location.get("address", {})

        if isinstance(address, str):
            address_text = address
            address = {}
        elif isinstance(address, dict):
            address_text = ""
        else:
            address = {}
            address_text = ""

        return {
            "name": clean_text(obj.get("name")),
            "start": obj.get("startDate"),
            "end": obj.get("endDate"),
            "venue": clean_text(location.get("name")),
            "street": clean_text(address.get("streetAddress")),
            "city": clean_text(address.get("addressLocality")),
            "state": clean_text(address.get("addressRegion")),
            "zip": clean_text(address.get("postalCode")),
            "address_text": address_text,
        }

    return {}


# ============================================================
# LISTING PAGE EXTRACTION
# ============================================================

def get_listing_date(anchor):
    """
    Find the date heading immediately associated with a listing.
    """
    date_pattern = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
        r"[A-Za-z]+\s+\d{1,2},\s+\d{4}",
        re.IGNORECASE,
    )

    previous_dates = anchor.find_all_previous(
        string=date_pattern
    )

    if not previous_dates:
        return None

    return parse_date(previous_dates[0])


def get_listing_location(anchor):
    """
    Extract the city from the surrounding TCDB listing.
    This is optional and is used only as address information.
    """
    city_pattern = re.compile(
        r"^(.+?),\s*TX(?:\s+\d{5})?$",
        re.IGNORECASE,
    )

    for parent in anchor.parents:
        if parent.name in ("body", "html"):
            break

        lines = [
            clean_text(line)
            for line in parent.get_text(
                "\n", strip=True
            ).splitlines()
            if clean_text(line)
        ]

        for line in lines:
            match = city_pattern.match(line)

            if match:
                return normalize_city(match.group(1))

    return None


def get_event_links(soup):
    """
    Collect unique TCDB event-detail links.
    """
    results = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]

        if "CardShows.cfm" not in href:
            continue

        if not re.search(
            r"(?:\?|&)ID=\d+",
            href,
            re.IGNORECASE,
        ):
            continue

        name = clean_text(
            anchor.get_text(" ", strip=True)
        )

        if not name:
            continue

        url = urljoin(BASE_URL, href)

        if url in seen_urls:
            continue

        seen_urls.add(url)

        results.append({
            "url": url,
            "name": name,
            "listing_start": get_listing_date(anchor),
            "listing_city": get_listing_location(anchor),
        })

    return results


# ============================================================
# EVENT DETAIL PAGE EXTRACTION
# ============================================================

def extract_city_from_text(text):
    """
    Look for a Texas city/state/ZIP pattern.
    City extraction is optional. It is no longer used
    to assign regions or reject events.
    """
    match = re.search(
        r"([A-Za-z][A-Za-z .'-]+),\s*TX\s+\d{5}",
        text,
        re.IGNORECASE,
    )

    if match:
        return normalize_city(match.group(1))

    match = re.search(
        r"([A-Za-z][A-Za-z .'-]+),\s*TX\b",
        text,
        re.IGNORECASE,
    )

    if match:
        return normalize_city(match.group(1))

    return None


def extract_address_from_text(soup):
    """
    Fallback address extraction from visible page text.
    """
    lines = [
        clean_text(line)
        for line in soup.get_text(
            "\n", strip=True
        ).splitlines()
        if clean_text(line)
    ]

    for index, line in enumerate(lines):
        if re.search(
            r",\s*TX\s+\d{5}\b",
            line,
            re.IGNORECASE,
        ):
            parts = []

            if index >= 2:
                parts.append(lines[index - 2])
                parts.append(lines[index - 1])

            elif index >= 1:
                parts.append(lines[index - 1])

            parts.append(line)

            return ", ".join(
                part for part in parts if part
            )

    return ""


def fetch_event_detail(session, listing):
    """
    Fetch one event's detail page and extract:
    event name, dates, venue, and address.

    Region lookup and city validation have been removed.
    """
    response = session.get(
        listing["url"],
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    page_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    structured = extract_jsonld_event(soup)

    # Event name
    event_name = (
        structured.get("name")
        or listing["name"]
    )

    # Dates
    start_date = parse_date(
        structured.get("start")
    )

    end_date = parse_date(
        structured.get("end")
    )

    if not start_date:
        start_date = listing["listing_start"]

    if not end_date:
        range_start, range_end = parse_date_range(
            page_text
        )

        if range_start and range_end:
            if not start_date:
                start_date = range_start

            end_date = range_end

    if not start_date:
        raise ValueError(
            f"No start date found for {event_name}"
        )

    # If TCDB provides no end date, treat it as a
    # single-day event.
    if not end_date:
        end_date = start_date

    # Address and city
    city = (
        structured.get("city")
        or listing.get("listing_city")
        or extract_city_from_text(page_text)
    )

    street = structured.get("street", "")
    venue = structured.get("venue", "")
    state = structured.get("state", "")
    postal_code = structured.get("zip", "")

    if state and state.upper() not in ("TX", "TEXAS"):
        raise ValueError(
            f"Event does not appear to be in Texas: "
            f"{event_name} ({state})"
        )

    # Use visible-page address as a fallback.
    fallback_address = ""

    if not street or not city:
        fallback_address = extract_address_from_text(soup)

    if not street and fallback_address:
        street = fallback_address

    if not city and fallback_address:
        city = extract_city_from_text(fallback_address)

    # Construct Venue/Address.
    # Missing city or street address will NOT cause
    # the event to be rejected.
    address_parts = []

    if venue:
        address_parts.append(venue)

    if street and street.casefold() not in [
        part.casefold() for part in address_parts
    ]:
        address_parts.append(street)

    if city:
        city_state_zip = city + ", TX"

        if postal_code:
            city_state_zip += " " + postal_code

        if not any(
            city.casefold() in part.casefold()
            for part in address_parts
        ):
            address_parts.append(city_state_zip)

    venue_address = ", ".join(
        part for part in address_parts if part
    )

    return {
        "Start": start_date,
        "End": end_date,
        "Event": event_name,
        "Venue/Address": venue_address,
    }


# ============================================================
# SCRAPE ALL TCDB EVENTS
# ============================================================

def scrape_tcdb():
    session = create_session()

    print("Loading TCDB Texas event listing...")

    response = session.get(
        TCDB_URL,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    listings = get_event_links(soup)

    if not listings:
        raise RuntimeError(
            "No TCDB event links were found. "
            "The TCDB page layout may have changed."
        )

    print(f"Event links found: {len(listings)}")

    events = []
    skipped = []
    seen = set()

    for index, listing in enumerate(listings, start=1):
        print(
            f"Processing {index}/{len(listings)}: "
            f"{listing['name']}"
        )

        try:
            event = fetch_event_detail(
                session,
                listing,
            )

            key = event_key(event)

            if key not in seen:
                events.append(event)
                seen.add(key)

        except Exception as exc:
            skipped.append(
                f"{listing['name']} | "
                f"{listing['url']} | {exc}"
            )

            print(f"  FAILED: {exc}")

        # Delay between detail-page requests.
        time.sleep(REQUEST_DELAY)

    if not events:
        raise RuntimeError(
            "No events could be successfully collected."
        )

    events.sort(
        key=lambda item: (
            datetime.strptime(
                item["Start"], "%m/%d/%y"
            ),
            item["Event"].casefold(),
        )
    )

    return events, len(listings), skipped


# ============================================================
# CSV FILE WRITING
# ============================================================

def write_csv(path, rows, columns):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# SNAPSHOT MANAGEMENT
# ============================================================

def read_snapshot():
    """
    Read the prior snapshot only if it uses the current
    four-column format.

    An older five-column snapshot is treated as a new
    baseline to prevent false additions/removals.
    """
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

        rows = []

        for row in reader:
            rows.append({
                column: row.get(column, "")
                for column in COLUMNS
            })

        return rows, True


def update_snapshot(events):
    """
    Write snapshot to a temporary file first,
    then replace the previous snapshot.
    """
    temp_file = SNAPSHOT_FILE.with_suffix(".tmp")

    write_csv(
        temp_file,
        events,
        COLUMNS,
    )

    temp_file.replace(SNAPSHOT_FILE)


# ============================================================
# DAILY CHANGE DETECTION
# ============================================================

def calculate_changes(current, previous):
    current_map = {
        event_key(event): event
        for event in current
    }

    previous_map = {
        event_key(event): event
        for event in previous
    }

    changes = []

    for key in sorted(
        current_map.keys() - previous_map.keys()
    ):
        row = current_map[key].copy()
        row["Change"] = "NEW"
        changes.append(row)

    for key in sorted(
        previous_map.keys() - current_map.keys()
    ):
        row = previous_map[key].copy()
        row["Change"] = "REMOVED"
        changes.append(row)

    return changes


# ============================================================
# AUDIT REPORT
# ============================================================

def write_audit(
    status,
    found=0,
    collected=0,
    skipped=None,
    new_count=0,
    removed_count=0,
    error=None,
    baseline=False,
):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with AUDIT_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        file.write(
            "TEXAS SHOW LOCATOR - SCRAPE AUDIT\n"
        )

        file.write("=" * 50 + "\n")
        file.write(
            f"Run time: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        file.write("Source: TCDB Texas Card Shows\n")
        file.write(f"Status: {status}\n")
        file.write(f"Event links found: {found}\n")
        file.write(f"Events collected: {collected}\n")
        file.write(f"New events: {new_count}\n")
        file.write(f"Removed events: {removed_count}\n")

        if baseline:
            file.write(
                "\nA new snapshot baseline was created. "
                "No changes were reported for this run.\n"
            )

        if error:
            file.write(f"\nERROR:\n{error}\n")

        if skipped:
            file.write(
                "\nEVENTS THAT COULD NOT BE PROCESSED:\n"
            )

            for item in skipped:
                file.write(f"- {item}\n")


# ============================================================
# MAIN
# ============================================================

def main():
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print("=" * 55)
    print("TEXAS SHOW LOCATOR")
    print("TCDB CSV SCRAPER - FOUR COLUMNS")
    print("=" * 55)

    # 1. Scrape TCDB.
    try:
        events, found, skipped = scrape_tcdb()

    except Exception as exc:
        message = str(exc)

        print(f"\nSCRAPE FAILED: {message}")
        print("Previous snapshot was not changed.")

        write_audit(
            status="FAILED - SNAPSHOT PRESERVED",
            error=message,
        )

        sys.exit(1)

    # 2. Do not publish an incomplete scrape.
    if skipped:
        message = (
            f"{len(skipped)} of {found} event listings "
            "could not be processed. No reports or "
            "snapshot were updated."
        )

        print(f"\nSCRAPE FAILED: {message}")
        print("Previous snapshot was preserved.")

        write_audit(
            status="FAILED - SNAPSHOT PRESERVED",
            found=found,
            collected=len(events),
            skipped=skipped,
            error=message,
        )

        sys.exit(1)

    # 3. Load the previous snapshot.
    previous, compatible_snapshot = read_snapshot()

    # 4. First run or old-format snapshot:
    # establish a fresh baseline.
    if not compatible_snapshot:
        changes = []
        baseline = True
        new_count = 0
        removed_count = 0

    else:
        changes = calculate_changes(
            events,
            previous,
        )

        new_count = sum(
            1 for row in changes
            if row["Change"] == "NEW"
        )

        removed_count = sum(
            1 for row in changes
            if row["Change"] == "REMOVED"
        )

        baseline = False

    # 5. Write master CSV.
    write_csv(
        MAIN_CSV,
        events,
        COLUMNS,
    )

    # 6. Write daily changes CSV.
    write_csv(
        CHANGES_CSV,
        changes,
        CHANGE_COLUMNS,
    )

    # 7. Update snapshot only after successful scrape.
    update_snapshot(events)

    # 8. Write audit report.
    write_audit(
        status="SUCCESS",
        found=found,
        collected=len(events),
        skipped=skipped,
        new_count=new_count,
        removed_count=removed_count,
        baseline=baseline,
    )

    # 9. Print summary.
    print("\n" + "=" * 55)
    print("SCRAPE COMPLETED SUCCESSFULLY")
    print("=" * 55)

    print(f"Event links found: {found}")
    print(f"Events collected: {len(events)}")
    print(f"New events: {new_count}")
    print(f"Removed events: {removed_count}")

    if baseline:
        print("Snapshot: NEW BASELINE CREATED")

    print("\nFILES CREATED:")
    print(f"Master CSV: {MAIN_CSV}")
    print(f"Daily changes: {CHANGES_CSV}")
    print(f"Audit report: {AUDIT_FILE}")
    print(f"Snapshot: {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()

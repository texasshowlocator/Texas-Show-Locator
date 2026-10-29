import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


# ============================================================
# TEXAS SHOW LOCATOR — EVENT SCRAPER AND CHANGE TRACKER
# ============================================================

TCDB_URL = (
    "https://www.tcdb.com/CardShows.cfm?"
    "MODE=Location&State=TX&Display=Texas&Country=United%20States"
)

ONTREASURE_URL = "https://www.ontreasure.com/events?states=TX"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 30

OUTPUT_DIR = Path("output")
SNAPSHOT_FILE = Path("data/previous_snapshot.csv")

COLUMNS = [
    "Start Date",
    "End Date",
    "Event Name",
    "Event Location",
    "Source Website",
]

CHANGE_COLUMNS = [
    "Change Type",
    "Start Date",
    "End Date",
    "Event Name",
    "Event Location",
    "Source Website",
]

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)

MONTH_PATTERN = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|Jan|Feb|Mar|Apr|"
    r"Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean(value):
    """Normalize whitespace and remove empty/null values."""
    if value is None:
        return ""

    value = str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    if value.strip().lower() in {"none", "null", "nan"}:
        return ""

    return value.strip()


def normalize(value):
    """Normalize text for comparisons."""
    value = clean(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_soup(url):
    """Download a page and return its BeautifulSoup document."""
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


def parse_date(value):
    """Convert common date strings to YYYY-MM-DD."""
    if value is None:
        return ""

    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")

    value = clean(value)

    if not value:
        return ""

    # Handle ISO timestamps, including timezone suffixes.
    try:
        parsed = pd.to_datetime(value, errors="raise", utc=True)
        return parsed.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        pass

    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d %B %Y",
        "%d %b %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""


def parse_month_day(value, default_year=None):
    """Parse a month/day date, optionally using a supplied year."""
    value = clean(value)

    if not value:
        return ""

    value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,")

    if default_year is not None and not re.search(r"\b20\d{2}\b", value):
        value = f"{value}, {default_year}"

    parsed = parse_date(value)

    if parsed:
        return parsed

    return ""


def make_record(
    start_date="",
    end_date="",
    event_name="",
    event_location="",
    source="",
):
    """Build a consistently shaped event record."""
    start_date = parse_date(start_date) if start_date else ""
    end_date = parse_date(end_date) if end_date else ""

    if not end_date:
        end_date = start_date

    return {
        "Start Date": start_date,
        "End Date": end_date,
        "Event Name": clean(event_name),
        "Event Location": clean(event_location),
        "Source Website": clean(source),
    }


def normalize_event_frame(frame):
    """Clean, standardize, sort, and deduplicate event records."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=COLUMNS)

    frame = frame.copy()

    # Migrate the old snapshot column name, if present.
    if "Location and Address" in frame.columns:
        if "Event Location" not in frame.columns:
            frame = frame.rename(
                columns={"Location and Address": "Event Location"}
            )
        else:
            frame["Event Location"] = (
                frame["Event Location"]
                .fillna("")
                .where(
                    frame["Event Location"].fillna("").astype(str).str.strip()
                    != "",
                    frame["Location and Address"],
                )
            )

    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    frame = frame[COLUMNS]

    for column in COLUMNS:
        frame[column] = frame[column].map(clean)

    frame["Start Date"] = frame["Start Date"].map(parse_date)
    frame["End Date"] = frame["End Date"].map(parse_date)

    # A missing end date defaults to the start date.
    frame.loc[
        frame["End Date"] == "", "End Date"
    ] = frame.loc[
        frame["End Date"] == "", "Start Date"
    ]

    frame = frame[
        (frame["Event Name"] != "")
        & (frame["Start Date"] != "")
    ].copy()

    # Remove exact duplicates without merging different sources.
    frame["_dedupe"] = frame.apply(
        lambda row: (
            normalize(row["Start Date"]),
            normalize(row["End Date"]),
            normalize(row["Event Name"]),
            normalize(row["Event Location"]),
            normalize(row["Source Website"]),
        ),
        axis=1,
    )

    frame = frame.drop_duplicates("_dedupe").drop(columns="_dedupe")

    return frame.sort_values(
        by=["Start Date", "Event Name", "Event Location"],
        kind="stable",
    ).reset_index(drop=True)


def event_key(row):
    """Comparison key for identifying the same event across snapshots."""
    return (
        normalize(row.get("Start Date", "")),
        normalize(row.get("Event Name", "")),
        normalize(row.get("Event Location", "")),
    )


def row_counter(frame):
    """Create a multiset of event keys."""
    return Counter(event_key(row) for _, row in frame.iterrows())


# ============================================================
# TCDB SCRAPER
# ============================================================

def extract_tcdb_date(text):
    """
    Find a full date in TCDB page text.
    Returns YYYY-MM-DD or an empty string.
    """
    text = clean(text)

    patterns = [
        rf"\b({MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?"
        rf"(?:\s*[-–—]\s*\d{{1,2}}(?:st|nd|rd|th)?)?"
        rf",?\s+20\d{{2}}\b",
        r"\b\d{1,2}/\d{1,2}/20\d{2}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)

        if match:
            found = match.group(0)
            found = re.sub(
                r"(\d)(st|nd|rd|th)\b",
                r"\1",
                found,
                flags=re.I,
            )

            # If a date range is present, the first date is the start.
            range_match = re.match(
                rf"({MONTH_PATTERN}\.?\s+\d{{1,2}})"
                rf"\s*[-–—]\s*\d{{1,2}},?\s+(20\d{{2}})",
                found,
                flags=re.I,
            )

            if range_match:
                return parse_month_day(
                    f"{range_match.group(1)}, {range_match.group(2)}"
                )

            parsed = parse_date(found)

            if parsed:
                return parsed

    return ""


def tcdb_location_from_text(text):
    """Extract a likely Texas venue/location line from listing text."""
    lines = [
        clean(line)
        for line in text.splitlines()
        if clean(line)
    ]

    for line in lines:
        if re.search(r"\bTX\b", line, flags=re.I):
            if len(line) <= 250:
                return line

    # Try city, Texas wording when the abbreviation isn't present.
    for line in lines:
        if re.search(r"\bTexas\b", line, flags=re.I):
            if len(line) <= 250:
                return line

    return ""


def scrape_tcdb():
    """
    Scrape TCDB's Texas card-show listing.

    This parser deliberately uses multiple HTML patterns because the
    listing's markup may change. It does not treat an empty result as
    a successful scrape.
    """
    soup = fetch_soup(TCDB_URL)
    records = []

    # Inspect links that look like individual TCDB card show pages.
    event_links = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        full_url = urljoin(TCDB_URL, href)
        parsed = urlparse(full_url)

        if parsed.netloc.lower() not in {
            "tcdb.com",
            "www.tcdb.com",
        }:
            continue

        if "CardShows.cfm" not in parsed.path:
            continue

        if full_url.split("#")[0] == TCDB_URL:
            continue

        # Individual card show pages commonly contain an ID parameter.
        if not re.search(r"(?:^|&)ID=\d+", parsed.query, flags=re.I):
            continue

        title = clean(anchor.get_text(" ", strip=True))

        if title:
            event_links.append((full_url, title, anchor))

    seen_urls = set()

    for url, link_title, anchor in event_links:
        if url in seen_urls:
            continue

        seen_urls.add(url)

        # Use nearby listing markup to get dates and location.
        container = anchor

        for _ in range(5):
            if container.parent is None:
                break

            container = container.parent
            container_text = clean(
                container.get_text(" ", strip=True)
            )

            if (
                extract_tcdb_date(container_text)
                and len(container_text) < 2500
            ):
                break

        container_text = container.get_text("\n", strip=True)

        start_date = extract_tcdb_date(container_text)
        location = tcdb_location_from_text(container_text)

        # Try the event detail page if the listing container lacks details.
        detail_soup = None

        if not start_date or not location:
            try:
                detail_soup = fetch_soup(url)
                detail_text = detail_soup.get_text("\n", strip=True)

                if not start_date:
                    start_date = extract_tcdb_date(detail_text)

                if not location:
                    location = tcdb_location_from_text(detail_text)

                if not link_title:
                    title_tag = detail_soup.find("h1") or detail_soup.title
                    if title_tag:
                        link_title = clean(
                            title_tag.get_text(" ", strip=True)
                        )
            except requests.RequestException as exc:
                print(f"TCDB detail-page warning: {url}: {exc}")

        # Skip entries that do not have enough information to identify
        # a dated event.
        if not start_date or not link_title:
            continue

        records.append(
            make_record(
                start_date=start_date,
                end_date=start_date,
                event_name=link_title,
                event_location=location,
                source="TCDB",
            )
        )

    # Some TCDB pages expose their events as text rather than individual
    # links. Use a fallback parser for date-headed listing sections.
    if not records:
        text = soup.get_text("\n", strip=True)
        lines = [clean(line) for line in text.splitlines()]
        lines = [line for line in lines if line]

        current_date = ""
        current_event = ""

        for index, line in enumerate(lines):
            found_date = extract_tcdb_date(line)

            if found_date:
                current_date = found_date
                current_event = ""
                continue

            if not current_date:
                continue

            # Candidate event names should not be navigation labels.
            if (
                len(line) >= 4
                and len(line) <= 180
                and not re.search(
                    r"^(home|login|register|search|next|previous|"
                    r"calendar|card shows|contact|about)$",
                    line,
                    flags=re.I,
                )
            ):
                current_event = line

                location = ""

                for next_line in lines[index + 1:index + 5]:
                    if re.search(r"\b(?:TX|Texas)\b", next_line, re.I):
                        location = next_line
                        break

                if current_event:
                    records.append(
                        make_record(
                            start_date=current_date,
                            end_date=current_date,
                            event_name=current_event,
                            event_location=location,
                            source="TCDB",
                        )
                    )
                    current_date = ""
                    current_event = ""

    result = normalize_event_frame(pd.DataFrame(records, columns=COLUMNS))

    if result.empty:
        raise RuntimeError(
            "TCDB returned no usable events. The page may have changed "
            "or its listing format may need an update. The previous "
            "snapshot will not be replaced."
        )

    return result


# ============================================================
# ONTREASURE SCRAPER
# ============================================================

def get_event_links(soup):
    """
    Collect unique Texas event detail URLs from OnTreasure.

    The Texas events page may display featured events outside Texas.
    This filter only accepts event links whose visible link text
    explicitly contains a Texas location, such as 'Houston, TX'.
    """
    links = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        url = urljoin(ONTREASURE_URL, anchor["href"])
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        link_text = clean(anchor.get_text(" ", strip=True))

        if parsed.netloc.lower() not in (
            "ontreasure.com",
            "www.ontreasure.com",
        ):
            continue

        if not path.startswith("/events/"):
            continue

        if path.endswith("/tickets"):
            continue

        # Exclude featured events unless their link text identifies Texas.
        if not re.search(r",\s*TX\b", link_text, re.IGNORECASE):
            continue

        if url not in seen:
            seen.add(url)
            links.append(url)

    return links


def jsonld_objects(soup):
    """Return JSON-LD objects, including objects nested in @graph."""
    objects = []

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"ld\+json", re.I)},
    ):
        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(data, list):
            objects.extend(data)
        elif isinstance(data, dict):
            objects.append(data)

            graph = data.get("@graph", [])
            if isinstance(graph, list):
                objects.extend(
                    item for item in graph if isinstance(item, dict)
                )

    return objects


def find_jsonld_events(soup):
    """Find Event objects from JSON-LD data."""
    events = []

    for item in jsonld_objects(soup):
        item_type = item.get("@type", "")

        if isinstance(item_type, list):
            item_type = " ".join(str(x) for x in item_type)

        if re.search(r"\bEvent\b", str(item_type), re.I):
            events.append(item)

    return events


def get_nested_name(value):
    """Extract a name from a JSON-LD name or object."""
    if isinstance(value, dict):
        return clean(value.get("name", ""))

    return clean(value)


def get_jsonld_location(event):
    """Extract a venue and city/state from a JSON-LD event."""
    location = event.get("location", "")

    if isinstance(location, list):
        location = location[0] if location else ""

    if not isinstance(location, dict):
        return clean(location)

    name = clean(location.get("name", ""))
    address = location.get("address", "")

    if isinstance(address, dict):
        parts = [
            clean(address.get("streetAddress", "")),
            clean(address.get("addressLocality", "")),
            clean(address.get("addressRegion", "")),
            clean(address.get("postalCode", "")),
        ]
        address_text = ", ".join(part for part in parts if part)
    else:
        address_text = clean(address)

    return ", ".join(part for part in [name, address_text] if part)


def is_texas_location(location):
    """Check whether a location clearly identifies Texas."""
    location = clean(location)

    return bool(
        re.search(r"(?:,\s*TX\b|\bTexas\b)", location, re.I)
    )


def parse_event_detail(url):
    """Extract event details from an OnTreasure event page."""
    soup = fetch_soup(url)
    event_objects = find_jsonld_events(soup)

    title = ""
    start_date = ""
    end_date = ""
    location = ""

    # Prefer structured event data.
    for event in event_objects:
        if not title:
            title = clean(event.get("name", ""))

        if not start_date:
            start_date = parse_date(event.get("startDate", ""))

        if not end_date:
            end_date = parse_date(event.get("endDate", ""))

        if not location:
            location = get_jsonld_location(event)

    # Page heading fallback.
    if not title:
        heading = soup.find("h1")

        if heading:
            title = clean(heading.get_text(" ", strip=True))

        if not title and soup.title:
            title = clean(soup.title.get_text(" ", strip=True))

    page_text = soup.get_text("\n", strip=True)

    # Try to locate dates in visible page text if JSON-LD lacks them.
    if not start_date:
        start_date, end_date = parse_date_range(page_text)

    # Location fallback: find a city/state line in the page text.
    if not location:
        for line in page_text.splitlines():
            line = clean(line)

            if is_texas_location(line) and len(line) <= 250:
                location = line
                break

    # Keep Texas-only events.
    if not is_texas_location(location):
        raise ValueError(
            f"Could not verify a Texas location for OnTreasure event: "
            f"{title or url}. Location found: {location or 'not found'}"
        )

    if not start_date:
        raise ValueError(
            f"Could not determine the start date for OnTreasure event: "
            f"{title or url}"
        )

    if not title:
        raise ValueError(
            f"Could not determine the event name for {url}"
        )

    if not end_date:
        end_date = start_date

    return make_record(
        start_date=start_date,
        end_date=end_date,
        event_name=title,
        event_location=location,
        source="OnTreasure",
    )


def parse_date_range(text):
    """
    Find a date or date range in visible text.
    Returns (start_date, end_date).
    """
    text = clean(text)

    # Month Day-Day, Year, e.g. October 10-11, 2026.
    range_patterns = [
        rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?\s*[-–—]\s*(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",

        rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(20\d{{2}})"
        rf"\s*(?:to|through|[-–—])\s*"
        rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
    ]

    match = re.search(range_patterns[0], text, re.I)

    if match:
        month, first_day, last_day, year = match.groups()
        start = parse_month_day(f"{month} {first_day}, {year}")
        end = parse_month_day(f"{month} {last_day}, {year}")

        if start:
            return start, end or start

    match = re.search(range_patterns[1], text, re.I)

    if match:
        month1, day1, year1, month2, day2, year2 = match.groups()
        start = parse_month_day(f"{month1} {day1}, {year1}")
        end = parse_month_day(f"{month2} {day2}, {year2}")

        if start:
            return start, end or start

    # Single full date.
    match = re.search(
        rf"\b{MONTH_PATTERN}\.?\s+\d{{1,2}}"
        rf"(?:st|nd|rd|th)?,?\s+20\d{{2}}\b",
        text,
        re.I,
    )

    if match:
        found = re.sub(
            r"(\d)(st|nd|rd|th)\b",
            r"\1",
            match.group(0),
            flags=re.I,
        )
        parsed = parse_date(found)

        if parsed:
            return parsed, parsed

    # ISO date.
    match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text)

    if match:
        parsed = parse_date(match.group(0))

        if parsed:
            return parsed, parsed

    return "", ""


def scrape_ontreasure():
    """Scrape Texas events from OnTreasure."""
    soup = fetch_soup(ONTREASURE_URL)
    event_urls = get_event_links(soup)

    if not event_urls:
        raise RuntimeError(
            "No Texas event links were found on OnTreasure. "
            "The listing markup may have changed, or the Texas location "
            "may not appear in the link text. The previous snapshot "
            "will not be replaced."
        )

    records = []
    errors = []

    for url in event_urls:
        try:
            record = parse_event_detail(url)
            records.append(record)
            print(f"OnTreasure OK: {record['Event Name']}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print(f"OnTreasure warning: {url}: {exc}")

    # Fail the source if any discovered event couldn't be parsed.
    # This prevents a partial scrape from falsely reporting removals.
    if errors:
        error_summary = "\n".join(errors[:10])
        raise RuntimeError(
            f"OnTreasure found {len(event_urls)} event links, but "
            f"{len(errors)} could not be verified or parsed. "
            f"To avoid false removals, the previous snapshot will "
            f"not be replaced.\n{error_summary}"
        )

    result = normalize_event_frame(pd.DataFrame(records, columns=COLUMNS))

    if result.empty:
        raise RuntimeError(
            "OnTreasure returned no usable Texas events. "
            "The previous snapshot will not be replaced."
        )

    return result


# ============================================================
# SNAPSHOT AND CHANGE COMPARISON
# ============================================================

def load_previous_snapshot():
    """Load the last successful combined event snapshot."""
    if not SNAPSHOT_FILE.exists():
        return pd.DataFrame(columns=COLUMNS)

    previous = pd.read_csv(
        SNAPSHOT_FILE,
        dtype=str,
        keep_default_na=False,
    )

    return normalize_event_frame(previous)


def compare_snapshots(previous, current):
    """
    Compare snapshots by start date, event name, and location.

    End dates are included in the report but not used as the primary
    identity key, so a changed end date doesn't automatically create
    a removal plus a new event.
    """
    changes = []

    old_counts = row_counter(previous)
    new_counts = row_counter(current)

    old_rows = {}
    new_rows = {}

    for _, row in previous.iterrows():
        key = event_key(row)
        old_rows.setdefault(key, []).append(row.to_dict())

    for _, row in current.iterrows():
        key = event_key(row)
        new_rows.setdefault(key, []).append(row.to_dict())

    all_keys = set(old_counts) | set(new_counts)

    for key in all_keys:
        old_count = old_counts.get(key, 0)
        new_count = new_counts.get(key, 0)

        if new_count > old_count:
            for row in new_rows[key][old_count:new_count]:
                changes.append(
                    {"Change Type": "NEW", **row}
                )

        elif old_count > new_count:
            for row in old_rows[key][new_count:old_count]:
                changes.append(
                    {"Change Type": "REMOVED", **row}
                )

    if not changes:
        return pd.DataFrame(columns=CHANGE_COLUMNS)

    result = pd.DataFrame(changes)

    for column in CHANGE_COLUMNS:
        if column not in result.columns:
            result[column] = ""

    return result[CHANGE_COLUMNS].sort_values(
        by=["Change Type", "Start Date", "Event Name"],
        kind="stable",
    ).reset_index(drop=True)


# ============================================================
# OUTPUT FILES
# ============================================================

def write_audit(lines):
    """Save a plain-text run audit."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    audit_file = OUTPUT_DIR / "scrape_audit.txt"
    audit_file.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def save_outputs(current, changes, audit_lines):
    """Write the combined list, change report, and audit file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    current.to_csv(
        OUTPUT_DIR / "texas_card_shows.csv",
        index=False,
        encoding="utf-8-sig",
    )

    changes.to_csv(
        OUTPUT_DIR / "daily_changes.csv",
        index=False,
        encoding="utf-8-sig",
    )

    with pd.ExcelWriter(
        OUTPUT_DIR / "texas_card_shows.xlsx",
        engine="openpyxl",
    ) as writer:
        current.to_excel(
            writer,
            sheet_name="All Texas Shows",
            index=False,
        )

        changes.to_excel(
            writer,
            sheet_name="Daily Changes",
            index=False,
        )

    changes.to_excel(
        OUTPUT_DIR / "daily_changes.xlsx",
        index=False,
    )

    write_audit(audit_lines)


def save_snapshot(current):
    """Persist the combined snapshot only after both sources succeed."""
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)

    current.to_csv(
        SNAPSHOT_FILE,
        index=False,
        encoding="utf-8-sig",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    audit_lines = [
        "Texas Show Locator — Scrape Audit",
        f"Run started: {started_at}",
        f"TCDB source: {TCDB_URL}",
        f"OnTreasure source: {ONTREASURE_URL}",
        "",
    ]

    print("=" * 65)
    print("TEXAS SHOW LOCATOR — DAILY EVENT SCRAPER")
    print("=" * 65)

    # Preserve the last successful snapshot if either source fails.
    try:
        print("\nScraping TCDB...")
        tcdb_events = scrape_tcdb()
        print(f"TCDB events collected: {len(tcdb_events)}")

        print("\nScraping OnTreasure...")
        ontreasure_events = scrape_ontreasure()
        print(f"OnTreasure events collected: {len(ontreasure_events)}")

    except Exception as exc:
        message = f"SOURCE FAILURE: {exc}"
        print(f"\nERROR: {message}")

        audit_lines.extend([
            message,
            "",
            "Snapshot was not replaced because one or more sources failed.",
            f"Run ended: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        ])

        write_audit(audit_lines)
        sys.exit(1)

    # Combine both sources. Same event from different sources is retained
    # because the source column helps with verification and attribution.
    combined = pd.concat(
        [tcdb_events, ontreasure_events],
        ignore_index=True,
    )

    combined = normalize_event_frame(combined)

    previous = load_previous_snapshot()
    is_first_run = previous.empty

    if is_first_run:
        changes = pd.DataFrame(columns=CHANGE_COLUMNS)
        audit_lines.append(
            "First successful run: no prior snapshot was available. "
            "All collected events are treated as the baseline."
        )
    else:
        changes = compare_snapshots(previous, combined)

    audit_lines.extend([
        "",
        f"TCDB event count: {len(tcdb_events)}",
        f"OnTreasure event count: {len(ontreasure_events)}",
        f"Combined event count: {len(combined)}",
        f"Previous snapshot event count: {len(previous)}",
        f"New event records: {int((changes['Change Type'] == 'NEW').sum()) if not changes.empty else 0}",
        f"Removed event records: {int((changes['Change Type'] == 'REMOVED').sum()) if not changes.empty else 0}",
        "",
        "Source status: SUCCESS",
        "Snapshot updated only after both sources completed successfully.",
        f"Run ended: {datetime.now().astimezone().isoformat(timespec='seconds')}",
    ])

    # Write reports first, then update the persistent snapshot.
    save_outputs(combined, changes, audit_lines)
    save_snapshot(combined)

    print("\n" + "=" * 65)
    print("SCRAPE COMPLETED SUCCESSFULLY")
    print("=" * 65)
    print(f"TCDB events: {len(tcdb_events)}")
    print(f"OnTreasure events: {len(ontreasure_events)}")
    print(f"Combined events: {len(combined)}")

    if is_first_run:
        print("First successful run: baseline snapshot created.")
    else:
        new_count = (
            int((changes["Change Type"] == "NEW").sum())
            if not changes.empty else 0
        )
        removed_count = (
            int((changes["Change Type"] == "REMOVED").sum())
            if not changes.empty else 0
        )

        print(f"New events: {new_count}")
        print(f"Removed events: {removed_count}")

    print(f"\nReports saved to: {OUTPUT_DIR.resolve()}")
    print(f"Snapshot saved to: {SNAPSHOT_FILE.resolve()}")


if __name__ == "__main__":
    main()

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
# TEXAS SHOW LOCATOR SETTINGS
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
    if value is None:
        return ""

    value = str(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    if value.strip().lower() in {"none", "null", "nan"}:
        return ""

    return value.strip()


def normalize(value):
    value = clean(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_soup(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(value):
    if value is None:
        return ""

    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")

    value = clean(value)

    if not value:
        return ""

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
    value = clean(value)

    if not value:
        return ""

    value = re.sub(
        r"(\d)(st|nd|rd|th)\b",
        r"\1",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" ,")

    if default_year is not None and not re.search(r"\b20\d{2}\b", value):
        value = f"{value}, {default_year}"

    return parse_date(value)


def make_record(
    start_date="",
    end_date="",
    event_name="",
    event_location="",
    source="",
):
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
    if frame is None or frame.empty:
        return pd.DataFrame(columns=COLUMNS)

    frame = frame.copy()

    # Support snapshots created by the earlier version of the scraper.
    if "Location and Address" in frame.columns:
        if "Event Location" not in frame.columns:
            frame = frame.rename(
                columns={"Location and Address": "Event Location"}
            )
        else:
            old_location = frame["Location and Address"].fillna("").astype(str)
            new_location = frame["Event Location"].fillna("").astype(str)

            frame["Event Location"] = new_location.where(
                new_location.str.strip() != "",
                old_location,
            )

    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    frame = frame[COLUMNS]

    for column in COLUMNS:
        frame[column] = frame[column].map(clean)

    frame["Start Date"] = frame["Start Date"].map(parse_date)
    frame["End Date"] = frame["End Date"].map(parse_date)

    blank_end = frame["End Date"] == ""
    frame.loc[blank_end, "End Date"] = frame.loc[blank_end, "Start Date"]

    frame = frame[
        (frame["Event Name"] != "")
        & (frame["Start Date"] != "")
    ].copy()

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
    return (
        normalize(row.get("Start Date", "")),
        normalize(row.get("Event Name", "")),
        normalize(row.get("Event Location", "")),
    )


def row_counter(frame):
    return Counter(event_key(row) for _, row in frame.iterrows())


# ============================================================
# TCDB SCRAPER
# ============================================================

def extract_tcdb_date(text):
    text = clean(text)

    patterns = [
        rf"\b({MONTH_PATTERN})\.?\s+\d{{1,2}}"
        rf"(?:st|nd|rd|th)?"
        rf"(?:\s*[-–—]\s*\d{{1,2}}(?:st|nd|rd|th)?)?"
        rf",?\s+20\d{{2}}\b",
        r"\b\d{1,2}/\d{1,2}/20\d{2}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)

        if not match:
            continue

        found = re.sub(
            r"(\d)(st|nd|rd|th)\b",
            r"\1",
            match.group(0),
            flags=re.I,
        )

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
    lines = [
        clean(line)
        for line in text.splitlines()
        if clean(line)
    ]

    for line in lines:
        if re.search(r"\bTX\b", line, flags=re.I) and len(line) <= 250:
            return line

    for line in lines:
        if re.search(r"\bTexas\b", line, flags=re.I) and len(line) <= 250:
            return line

    return ""


def scrape_tcdb():
    soup = fetch_soup(TCDB_URL)
    records = []
    event_links = []

    for anchor in soup.find_all("a", href=True):
        full_url = urljoin(TCDB_URL, anchor["href"])
        parsed = urlparse(full_url)

        if parsed.netloc.lower() not in {"tcdb.com", "www.tcdb.com"}:
            continue

        if "CardShows.cfm" not in parsed.path:
            continue

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

        container = anchor

        for _ in range(5):
            if container.parent is None:
                break

            container = container.parent
            container_text = clean(container.get_text(" ", strip=True))

            if extract_tcdb_date(container_text) and len(container_text) < 2500:
                break

        container_text = container.get_text("\n", strip=True)
        start_date = extract_tcdb_date(container_text)
        location = tcdb_location_from_text(container_text)

        if not start_date or not location:
            try:
                detail_soup = fetch_soup(url)
                detail_text = detail_soup.get_text("\n", strip=True)

                if not start_date:
                    start_date = extract_tcdb_date(detail_text)

                if not location:
                    location = tcdb_location_from_text(detail_text)

                if not link_title:
                    heading = detail_soup.find("h1") or detail_soup.title
                    if heading:
                        link_title = clean(heading.get_text(" ", strip=True))

            except requests.RequestException as exc:
                print(f"TCDB detail-page warning: {url}: {exc}")

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

    # Fallback if TCDB exposes its events as text rather than event links.
    if not records:
        lines = [
            clean(line)
            for line in soup.get_text("\n", strip=True).splitlines()
        ]
        lines = [line for line in lines if line]

        for index, line in enumerate(lines):
            found_date = extract_tcdb_date(line)

            if not found_date:
                continue

            for candidate in lines[index + 1:index + 5]:
                if (
                    len(candidate) >= 4
                    and len(candidate) <= 180
                    and not re.search(
                        r"^(home|login|register|search|next|previous|"
                        r"calendar|card shows|contact|about)$",
                        candidate,
                        re.I,
                    )
                ):
                    location = ""

                    for next_line in lines[index + 1:index + 8]:
                        if re.search(r"\b(?:TX|Texas)\b", next_line, re.I):
                            location = next_line
                            break

                    records.append(
                        make_record(
                            start_date=found_date,
                            end_date=found_date,
                            event_name=candidate,
                            event_location=location,
                            source="TCDB",
                        )
                    )
                    break

    result = normalize_event_frame(pd.DataFrame(records, columns=COLUMNS))

    if result.empty:
        raise RuntimeError(
            "TCDB returned no usable events. The page may have changed. "
            "The previous snapshot will not be replaced."
        )

    return result


# ============================================================
# ONTREASURE SCRAPER
# ============================================================

def get_event_links(soup):
    """
    Collect unique Texas event URLs and their city from the listing.

    The location is extracted from the event link and nearby card text.
    This helps when a detail page doesn't expose a venue address.
    """
    links = []
    seen = set()

    location_pattern = re.compile(
        r"\b([A-Za-z][A-Za-z .'-]*,\s*TX)\b",
        re.IGNORECASE,
    )

    for anchor in soup.find_all("a", href=True):
        url = urljoin(ONTREASURE_URL, anchor["href"])
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")

        if parsed.netloc.lower() not in (
            "ontreasure.com",
            "www.ontreasure.com",
        ):
            continue

        if not path.startswith("/events/") or path.endswith("/tickets"):
            continue

        # Look at the anchor and its surrounding event card.
        context_parts = []
        node = anchor

        for _ in range(4):
            if node is None:
                break

            context_parts.append(node.get_text(" ", strip=True))
            node = node.parent

        context = " ".join(context_parts)
        match = location_pattern.search(context)

        # Do not accept events unless a Texas city is visible.
        if not match:
            continue

        city = clean(match.group(1))
        city = re.sub(r"\s+", " ", city)

        if url not in seen:
            seen.add(url)
            links.append((url, city))

    return links


def jsonld_objects(soup):
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
    events = []

    for item in jsonld_objects(soup):
        item_type = item.get("@type", "")

        if isinstance(item_type, list):
            item_type = " ".join(str(value) for value in item_type)

        if re.search(r"\bEvent\b", str(item_type), re.I):
            events.append(item)

    return events


def get_jsonld_location(event):
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
    location = clean(location)

    return bool(
        re.search(r"(?:,\s*TX\b|\bTexas\b)", location, re.I)
    )


def parse_date_range(text):
    text = clean(text)

    # Example: October 31-November 1, 2026.
    match = re.search(
        rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?\s*[-–—]\s*"
        rf"({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
        text,
        re.I,
    )

    if match:
        month1, day1, month2, day2, year = match.groups()

        start = parse_month_day(f"{month1} {day1}, {year}")
        end = parse_month_day(f"{month2} {day2}, {year}")

        if start:
            return start, end or start

    # Example: October 31-November 1, 2026, with year only at end.
    match = re.search(
        rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?\s*[-–—]\s*(\d{{1,2}})"
        rf"(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
        text,
        re.I,
    )

    if match:
        month, day1, day2, year = match.groups()

        start = parse_month_day(f"{month} {day1}, {year}")
        end = parse_month_day(f"{month} {day2}, {year}")

        if start:
            return start, end or start

    # Example: October 31, 2026.
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

    # ISO date fallback.
    match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text)

    if match:
        parsed = parse_date(match.group(0))

        if parsed:
            return parsed, parsed

    return "", ""


def parse_event_detail(url, listing_location=""):
    soup = fetch_soup(url)
    event_objects = find_jsonld_events(soup)

    title = ""
    start_date = ""
    end_date = ""
    location = ""

    # Prefer structured data when available.
    for event in event_objects:
        if not title:
            title = clean(event.get("name", ""))

        if not start_date:
            start_date = parse_date(event.get("startDate", ""))

        if not end_date:
            end_date = parse_date(event.get("endDate", ""))

        if not location:
            location = get_jsonld_location(event)

    # Fall back to the visible heading.
    if not title:
        heading = soup.find("h1")

        if heading:
            title = clean(heading.get_text(" ", strip=True))

        if not title and soup.title:
            title = clean(soup.title.get_text(" ", strip=True))

    page_text = soup.get_text("\n", strip=True)

    # Extract dates from the visible page if structured dates are absent.
    if not start_date:
        start_date, end_date = parse_date_range(page_text)

    # Try to find a Texas location in the detail page.
    if not is_texas_location(location):
        for line in page_text.splitlines():
            line = clean(line)

            if is_texas_location(line) and len(line) <= 250:
                location = line
                break

    # Fallback to the Texas city identified on the listing page.
    if not is_texas_location(location) and is_texas_location(listing_location):
        location = listing_location

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


def scrape_ontreasure():
    soup = fetch_soup(ONTREASURE_URL)
    event_links = get_event_links(soup)

    if not event_links:
        raise RuntimeError(
            "No Texas event links were found on OnTreasure. "
            "The listing layout may have changed, or the city may not "
            "appear in the event card. The previous snapshot will not "
            "be replaced."
        )

    records = []
    errors = []

    for url, listing_location in event_links:
        try:
            record = parse_event_detail(url, listing_location)
            records.append(record)

            print(
                f"OnTreasure OK: {record['Event Name']} "
                f"({record['Event Location']})"
            )

        except Exception as exc:
            errors.append(f"{url}: {exc}")
            print(f"OnTreasure warning: {url}: {exc}")

    # Fail closed rather than treating a partial scrape as removals.
    if errors:
        error_summary = "\n".join(errors[:10])

        raise RuntimeError(
            f"OnTreasure found {len(event_links)} event links, but "
            f"{len(errors)} could not be verified or parsed. "
            f"The previous snapshot will not be replaced.\n"
            f"{error_summary}"
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
    Compare events by start date, event name, and location.
    End dates are reported but do not define event identity.
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

    for key in set(old_counts) | set(new_counts):
        old_count = old_counts.get(key, 0)
        new_count = new_counts.get(key, 0)

        if new_count > old_count:
            for row in new_rows[key][old_count:new_count]:
                changes.append({"Change Type": "NEW", **row})

        elif old_count > new_count:
            for row in old_rows[key][new_count:old_count]:
                changes.append({"Change Type": "REMOVED", **row})

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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    (OUTPUT_DIR / "scrape_audit.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def save_outputs(current, changes, audit_lines):
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
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)

    current.to_csv(
        SNAPSHOT_FILE,
        index=False,
        encoding="utf-8-sig",
    )


# ============================================================
# MAIN WORKFLOW
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

    # Do not update the previous snapshot if either source fails.
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
            "Snapshot was not replaced because a source failed.",
            (
                "Run ended: "
                f"{datetime.now().astimezone().isoformat(timespec='seconds')}"
            ),
        ])

        write_audit(audit_lines)
        sys.exit(1)

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
            "This run establishes the baseline."
        )
    else:
        changes = compare_snapshots(previous, combined)

    new_count = (
        int((changes["Change Type"] == "NEW").sum())
        if not changes.empty else 0
    )

    removed_count = (
        int((changes["Change Type"] == "REMOVED").sum())
        if not changes.empty else 0
    )

    audit_lines.extend([
        "",
        f"TCDB event count: {len(tcdb_events)}",
        f"OnTreasure event count: {len(ontreasure_events)}",
        f"Combined event count: {len(combined)}",
        f"Previous snapshot event count: {len(previous)}",
        f"New event records: {new_count}",
        f"Removed event records: {removed_count}",
        "",
        "Source status: SUCCESS",
        "Snapshot updated after both sources completed successfully.",
        (
            "Run ended: "
            f"{datetime.now().astimezone().isoformat(timespec='seconds')}"
        ),
    ])

    # Save reports first and update the persistent snapshot last.
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
        print(f"New events: {new_count}")
        print(f"Removed events: {removed_count}")

    print(f"\nReports saved to: {OUTPUT_DIR.resolve()}")
    print(f"Snapshot saved to: {SNAPSHOT_FILE.resolve()}")


if __name__ == "__main__":
    main()

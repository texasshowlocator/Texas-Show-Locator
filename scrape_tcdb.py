import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
import requests
from bs4 import BeautifulSoup


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

SOURCE_URL = (
    "https://www.tcdb.com/CardShows.cfm"
    "?MODE=Location&State=TX&Display=Texas"
    "&Country=United%20States"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TexasShowLocator/1.0)"
}

OUTPUT = Path("output")
OUTPUT.mkdir(exist_ok=True)


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def clean(text):
    """Remove extra whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def get_soup(url):
    """Download and parse a webpage."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(text):
    """Convert a recognizable date to YYYY-MM-DD."""
    text = clean(text)

    formats = [
        "%A, %B %d, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d %Y",
        "%b %d %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""


def extract_dates(text):
    """
    Extract a start and end date when both are shown.
    If only one date is available, use it for both columns.
    """
    text = clean(text)

    # Numeric range: 11/14/2026 - 11/15/2026
    match = re.search(
        r"\b(\d{1,2}/\d{1,2}/\d{2,4})\s*"
        r"(?:-|–|—|to)\s*"
        r"(\d{1,2}/\d{1,2}/\d{2,4})\b",
        text,
        re.IGNORECASE,
    )

    if match:
        start = parse_date(match.group(1))
        end = parse_date(match.group(2))

        if start:
            return start, end or start

    # Month-name range: November 14-15, 2026
    match = re.search(
        r"\b([A-Za-z]+)\s+(\d{1,2})\s*"
        r"(?:-|–|—|to)\s*(\d{1,2}),?\s+(\d{4})\b",
        text,
    )

    if match:
        month, start_day, end_day, year = match.groups()

        start = parse_date(f"{month} {start_day}, {year}")
        end = parse_date(f"{month} {end_day}, {year}")

        if start:
            return start, end or start

    # Single numeric date
    match = re.search(
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        text,
    )

    if match:
        date = parse_date(match.group(0))
        if date:
            return date, date

    # Single month-name date
    match = re.search(
        r"\b[A-Za-z]+\s+\d{1,2},?\s+\d{4}\b",
        text,
    )

    if match:
        date = parse_date(match.group(0))
        if date:
            return date, date

    # Weekday followed by a month-name date
    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|"
        r"Saturday|Sunday),?\s+"
        r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b",
        text,
        re.IGNORECASE,
    )

    if match:
        date = parse_date(match.group(1))
        if date:
            return date, date

    return "", ""


def extract_location(lines):
    """
    Extract venue and location details from the event page.
    Does not use Google Maps or any external address lookup.
    """
    location_lines = []
    started = False

    date_pattern = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|"
        r"Saturday|Sunday),?.*\d{4}",
        re.IGNORECASE,
    )

    stop_phrases = {
        "notes:",
        "going to this show?",
        "see any inaccuracies?",
        "add to calendar",
        "directions",
        "future dates",
        "past dates",
    }

    for line in lines:
        line = clean(line)

        if not line:
            continue

        if date_pattern.search(line):
            started = True
            continue

        if not started:
            continue

        if line.lower() in stop_phrases:
            break

        # Skip obvious navigation and unrelated labels.
        if line.lower() in {
            "home",
            "card shows",
            "united states",
            "usa",
        }:
            continue

        location_lines.append(line)

        # Avoid collecting large amounts of unrelated page text.
        if len(location_lines) >= 4:
            break

    # Remove consecutive duplicate lines.
    cleaned_lines = []

    for line in location_lines:
        if not cleaned_lines or line != cleaned_lines[-1]:
            cleaned_lines.append(line)

    return ", ".join(cleaned_lines)


def scrape_event(url, listing_name=""):
    """Extract an event's details from its TCDB page."""
    soup = get_soup(url)

    lines = [
        clean(line)
        for line in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [line for line in lines if line]

    page_text = clean(" ".join(lines))

    # Prefer the event name from the Texas listing.
    event_name = clean(listing_name)

    if not event_name:
        heading = soup.find(["h1", "h2", "h3"])

        if heading:
            event_name = clean(heading.get_text(" ", strip=True))
        elif soup.title:
            event_name = clean(soup.title.get_text())

    start_date, end_date = extract_dates(page_text)
    location = extract_location(lines)

    return {
        "Start Date": start_date,
        "End Date": end_date,
        "Event Name": event_name,
        "Location and Address": location,
    }


# --------------------------------------------------
# MAIN SCRAPER
# --------------------------------------------------

def main():
    print("Loading TCDB Texas card show listings...")

    soup = get_soup(SOURCE_URL)

    event_links = []

    for link in soup.find_all("a", href=True):
        event_name = clean(link.get_text(" ", strip=True))
        event_url = urljoin(SOURCE_URL, link["href"])

        parsed = urlparse(event_url)
        params = parse_qs(parsed.query)

        # Find links to individual TCDB card show records.
        if (
    parsed.netloc.lower().endswith("tcdb.com")
    and parsed.path.lower().endswith("cardshows.cfm")
    and params.get("ID")
):
        ):
            if event_name:
                event_links.append((event_name, event_url))

    # Remove duplicate event URLs.
    unique_events = []
    seen_urls = set()

    for event_name, event_url in event_links:
        if event_url not in seen_urls:
            seen_urls.add(event_url)
            unique_events.append((event_name, event_url))

    if not unique_events:
        raise RuntimeError(
            "No individual event links were found. "
            "The TCDB page structure may have changed."
        )

    print(f"Found {len(unique_events)} event links.")

    results = []

    for index, (event_name, event_url) in enumerate(
        unique_events,
        start=1,
    ):
        try:
            event = scrape_event(event_url, event_name)
            results.append(event)

            print(
                f"Processed {index}/{len(unique_events)}: "
                f"{event['Event Name']}"
            )

        except requests.RequestException as error:
            print(f"Could not process {event_url}: {error}")

    if not results:
        raise RuntimeError("No event details could be extracted.")

    # Keep exactly these four columns in this order.
    columns = [
        "Start Date",
        "End Date",
        "Event Name",
        "Location and Address",
    ]

    df = pd.DataFrame(results, columns=columns)

    df = df.drop_duplicates(
        subset=["Start Date", "End Date", "Event Name", "Location and Address"]
    )

    df = df.sort_values(
        by="Start Date",
        na_position="last",
        kind="stable",
    )

    csv_path = OUTPUT / "tcdb_texas_card_shows.csv"
    excel_path = OUTPUT / "tcdb_texas_card_shows.xlsx"

    df.to_csv(csv_path, index=False)
    df.to_excel(excel_path, index=False)

    print(f"\nFinished. Exported {len(df)} events.")
    print(f"Excel file: {excel_path}")
    print(f"CSV file: {csv_path}")


if __name__ == "__main__":
    main()

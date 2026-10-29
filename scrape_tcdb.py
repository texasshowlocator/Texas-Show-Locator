import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

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

def clean(value):
    """Remove extra spaces and clean text."""
    return re.sub(r"\s+", " ", value or "").strip()


def get_soup(url):
    """Download a webpage and turn it into searchable HTML."""
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(value):
    """Convert a recognizable date into YYYY-MM-DD."""
    value = clean(value)

    formats = [
        "%m/%d/%Y",
        "%m/%d/%y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%m/%d",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)

            # If the year is missing, use the current year.
            if "%Y" not in fmt and "%y" not in fmt:
                parsed = parsed.replace(year=datetime.now().year)

            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def extract_date_range(text):
    """
    Find a start date and optional end date in event text.

    Examples:
    11/14/2026
    11/14/2026 - 11/15/2026
    November 14-15, 2026
    """
    text = clean(text)

    # Numeric date range, such as 11/14/2026 - 11/15/2026
    match = re.search(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*"
        r"(?:-|–|—|to)\s*"
        r"(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.IGNORECASE,
    )

    if match:
        start_date = parse_date(match.group(1))
        end_date = parse_date(match.group(2))
        if start_date:
            return start_date, end_date or start_date

    # Single numeric date
    match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
    if match:
        start_date = parse_date(match.group(0))
        if start_date:
            return start_date, start_date

    # Month-name date range, such as November 14-15, 2026
    match = re.search(
        r"\b([A-Za-z]+)\s+(\d{1,2})\s*"
        r"(?:-|–|—|to)\s*(\d{1,2}),?\s+(\d{4})\b",
        text,
    )

    if match:
        month, start_day, end_day, year = match.groups()
        start_date = parse_date(f"{month} {start_day}, {year}")
        end_date = parse_date(f"{month} {end_day}, {year}")

        if start_date:
            return start_date, end_date or start_date

    # Single month-name date
    match = re.search(
        r"\b[A-Za-z]+\s+\d{1,2},?\s+\d{4}\b",
        text,
    )

    if match:
        start_date = parse_date(match.group(0))
        if start_date:
            return start_date, start_date

    return None, None


def scrape_event(url):
    """Read an individual event page and extract available details."""
    soup = get_soup(url)

    page_text = clean(soup.get_text(" ", strip=True))
    title = clean(soup.title.get_text()) if soup.title else ""

    start_date, end_date = extract_date_range(page_text)

    return {
        "Event Name": title,
        "Start Date": start_date or "",
        "End Date": end_date or start_date or "",
        "Event URL": url,
        "Page Text": page_text,
    }


# --------------------------------------------------
# MAIN SCRAPER
# --------------------------------------------------

def main():
    print("Opening the TCDB Texas card show listing...")

    soup = get_soup(SOURCE_URL)
    events = []

    # Find links to individual card show pages.
    for link in soup.find_all("a", href=True):
        event_name = clean(link.get_text(" ", strip=True))
        event_url = urljoin(SOURCE_URL, link["href"])

        if not event_name:
            continue

        if "CardShows.cfm" not in event_url:
            continue

        if event_url.rstrip("/") == SOURCE_URL.rstrip("/"):
            continue

        try:
            details = scrape_event(event_url)
        except requests.RequestException as error:
            print(f"Could not read {event_url}: {error}")
            continue

        # Prefer the link text for the event name when available.
        details["Event Name"] = event_name or details["Event Name"]
        details["Source Website"] = "TCDB"

        events.append(details)

    # Remove duplicate links.
    unique_events = []
    seen_urls = set()

    for event in events:
        if event["Event URL"] not in seen_urls:
            seen_urls.add(event["Event URL"])
            unique_events.append(event)

    if not unique_events:
        raise RuntimeError(
            "No events were found. The TCDB page may have changed "
            "or its event links may need a different extraction method."
        )

    df = pd.DataFrame(unique_events)

    csv_path = OUTPUT / "tcdb_texas_card_shows.csv"
    xlsx_path = OUTPUT / "tcdb_texas_card_shows.xlsx"

    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False)

    print(f"Found {len(df)} event links.")
    print(f"CSV saved to: {csv_path}")
    print(f"Excel saved to: {xlsx_path}")


if __name__ == "__main__":
    main()

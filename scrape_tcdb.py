import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
import requests
from bs4 import BeautifulSoup


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

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def clean(text):
    """Normalize whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def get_soup(url):
    """Download and parse a webpage."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(text):
    """Convert a date to YYYY-MM-DD."""
    text = clean(text)

    formats = [
        "%A, %B %d, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]

    from datetime import datetime

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return ""


def extract_dates(lines):
    """
    Find the event date from a line such as:
    Sunday, November 29, 2026 (10:00 AM - 4:00 PM)

    If the page provides only one date, use it for both
    start and end date. Multi-day events listed as separate
    TCDB entries may need to be combined later.
    """
    for line in lines:
        match = re.search(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|"
            r"Saturday|Sunday),?\s+"
            r"[A-Za-z]+\s+\d{1,2},?\s+\d{4}",
            line,
            re.IGNORECASE,
        )

        if match:
            date_text = re.sub(
                r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
                "",
                match.group(0),
                flags=re.IGNORECASE,
            )

            date_text = date_text.replace(",", ",")
            start_date = parse_date(match.group(0))

            if not start_date:
                start_date = parse_date(date_text)

            if start_date:
                return start_date, start_date

        # Numeric date, if used by the page.
        match = re.search(
            r"\b\d{1,2}/\d{1,2}/\d{4}\b",
            line
        )
        if match:
            date = parse_date(match.group(0))
            if date:
                return date, date

    return "", ""


def get_google_maps_address(query):
    """Look up an address using Google Maps Geocoding API."""
    if not GOOGLE_MAPS_API_KEY or not query:
        return ""

    url = "https://maps.googleapis.com/maps/api/geocode/json"

    try:
        response = requests.get(
            url,
            params={
                "address": query,
                "key": GOOGLE_MAPS_API_KEY,
                "region": "us",
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0].get("formatted_address", "")

    except requests.RequestException as error:
        print(f"Google Maps lookup failed: {error}")

    return ""


def extract_location(lines):
    """
    Extract venue and address lines from a TCDB event page.

    Returns a combined location/address string.
    """
    venue_lines = []
    started = False

    for line in lines:
        line = clean(line)

        # The date/time line normally appears before the venue.
        if re.search(
            r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),"
            r".*\d{4}",
            line,
            re.IGNORECASE,
        ):
            started = True
            continue

        if not started:
            continue

        if line.lower() in {"united states", "usa"}:
            break

        if line.lower() in {
            "notes:", "going to this show?", "see any inaccuracies?"
        }:
            break

        # Skip obvious page controls and irrelevant text.
        if line.lower() in {
            "add to calendar", "i'm going", "directions"
        }:
            continue

        # Stop if another date section begins.
        if re.search(r"future dates", line, re.IGNORECASE):
            break

        if line:
            venue_lines.append(line)

        # Usually enough to capture venue, street, and city/state/ZIP.
        if len(venue_lines) >= 4:
            break

    # Remove duplicate consecutive lines.
    cleaned_lines = []
    for line in venue_lines:
        if not cleaned_lines or line != cleaned_lines[-1]:
            cleaned_lines.append(line)

    location = ", ".join(cleaned_lines)

    # If we have only a venue or city without a street address,
    # attempt a Google Maps lookup.
    has_street_address = any(
        re.search(r"\d{1,6}\s+\S+", line)
        for line in cleaned_lines[1:]
    )

    if location and not has_street_address:
        address = get_google_maps_address(
            f"{location}, Texas"
        )

        if address:
            location = f"{location}, {address}"

    return location


def scrape_event(url):
    """Extract the requested fields from one event page."""
    soup = get_soup(url)

    lines = [
        clean(line)
        for line in soup.get_text("\n", strip=True).splitlines()
    ]
    lines = [line for line in lines if line]

    # The main event title is generally the first page heading.
    heading = soup.find(["h1", "h2", "h3"])

    if heading:
        event_name = clean(heading.get_text(" ", strip=True))
    else:
        event_name = ""

    # Fall back to the browser title if necessary.
    if not event_name and soup.title:
        event_name = clean(soup.title.get_text())

    start_date, end_date = extract_dates(lines)
    location = extract_location(lines)

    # A Google Maps lookup may also help when no location was found.
    if not location and event_name:
        location = get_google_maps_address(
            f"{event_name}, Texas card show"
        )

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
    event_urls = []

    for link in soup.find_all("a", href=True):
        href = urljoin(SOURCE_URL, link["href"])
        parsed = urlparse(href)
        params = parse_qs(parsed.query)

        # Only collect links to individual TCDB event records.
        if (
            parsed.netloc.lower().endswith("tcdb.com")
            and parsed.path.lower().endswith("cardshows.cfm")
            and params.get("ID")
            and params.get("MODE", [""])[0].upper() == "VIEW"
        ):
            event_urls.append(href)

    # Remove duplicate event links.
    event_urls = list(dict.fromkeys(event_urls))

    if not event_urls:
        raise RuntimeError(
            "No individual event links were found. "
            "The TCDB listing format may have changed."
        )

    print(f"Found {len(event_urls)} event links.")

    events = []

    for index, url in enumerate(event_urls, start=1):
        try:
            event = scrape_event(url)
            events.append(event)
            print(
                f"Processed {index}/{len(event_urls)}: "
                f"{event['Event Name']}"
            )
        except (requests.RequestException, ValueError) as error:
            print(f"Could not process {url}: {error}")

    if not events:
        raise RuntimeError("No event details could be extracted.")

    # EXACTLY four columns, in the requested order.
    columns = [
        "Start Date",
        "End Date",
        "Event Name",
        "Location and Address",
    ]

    df = pd.DataFrame(events, columns=columns)

    # Sort by start date, with undated entries at the bottom.
    df = df.sort_values(
        by="Start Date",
        na_position="last",
        kind="stable",
    )

    csv_path = OUTPUT / "tcdb_texas_card_shows.csv"
    xlsx_path = OUTPUT / "tcdb_texas_card_shows.xlsx"

    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False)

    print(f"Finished. Exported {len(df)} events.")
    print(f"Excel file: {xlsx_path}")
    print(f"CSV file: {csv_path}")


if __name__ == "__main__":
    main()

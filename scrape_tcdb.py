import re
from collections import defaultdict, deque
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
    """Normalize whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def normalize(text):
    """Normalize event names for matching."""
    return re.sub(r"[^a-z0-9]+", " ", clean(text).lower()).strip()


def get_soup(url):
    """Download and parse a webpage."""
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_listing_date(text):
    """
    Recognize date headings such as:
    Friday, October 30, 2026
    Saturday, November 7, 2026
    """
    text = clean(text)

    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|"
        r"Saturday|Sunday),?\s+"
        r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b",
        text,
        re.IGNORECASE,
    )

    if not match:
        return ""

    date_text = match.group(1)

    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(
                date_text, fmt
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return ""


def is_event_link(url):
    """Identify links to individual TCDB card show records."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    return (
        parsed.netloc.lower().endswith("tcdb.com")
        and parsed.path.lower().endswith("cardshows.cfm")
        and bool(params.get("ID"))
    )


def extract_event_links(soup):
    """
    Collect event links without requiring MODE=VIEW.
    Preserve repeated event names in the order they appear.
    """
    links_by_name = defaultdict(deque)

    for anchor in soup.find_all("a", href=True):
        name = clean(anchor.get_text(" ", strip=True))
        url = urljoin(SOURCE_URL, anchor["href"])

        if not name or not is_event_link(url):
            continue

        # Ignore links that point back to the listing itself.
        params = parse_qs(urlparse(url).query)

        if params.get("MODE", [""])[0].upper() == "LOCATION":
            continue

        links_by_name[normalize(name)].append(url)

    return links_by_name


def get_location(lines, start_index, event_names):
    """
    Read the venue and city immediately after an event name.
    Stop before the next event or date heading.
    """
    location_parts = []

    for line in lines[start_index + 1:start_index + 5]:
        line = clean(line)

        if not line:
            continue

        if parse_listing_date(line):
            break

        if normalize(line) in event_names:
            break

        # The city/state line normally looks like "Austin, TX".
        location_parts.append(line)

        if re.search(r",\s*[A-Z]{2}\b", line):
            break

    # Usually this combines the venue and city/state.
    return ", ".join(location_parts)


# --------------------------------------------------
# MAIN SCRAPER
# --------------------------------------------------

def main():
    print("Loading the complete TCDB Texas listing...")

    soup = get_soup(SOURCE_URL)

    # Use the listing page itself, rather than relying on individual
    # event pages to determine which events exist.
    raw_lines = soup.get_text("\n", strip=True).splitlines()
    lines = [clean(line) for line in raw_lines if clean(line)]

    links_by_name = extract_event_links(soup)
    event_names = set(links_by_name.keys())

    if not event_names:
        raise RuntimeError(
            "No event links were found on the TCDB listing. "
            "The website's link format may have changed."
        )

    print(f"Found {sum(len(q) for q in links_by_name.values())} event links.")

    events = []
    current_date = ""
    matched_links = 0

    for index, line in enumerate(lines):
        # Update the date whenever a new date heading appears.
        listing_date = parse_listing_date(line)

        if listing_date:
            current_date = listing_date
            continue

        normalized_name = normalize(line)

        # Only process lines matching an actual event link.
        if normalized_name not in links_by_name:
            continue

        if not links_by_name[normalized_name]:
            continue

        event_url = links_by_name[normalized_name].popleft()
        matched_links += 1

        location = get_location(
            lines,
            index,
            event_names,
        )

        # The listing groups shows by individual calendar date.
        # When only one date is available, use it in both date columns.
        events.append({
            "Start Date": current_date,
            "End Date": current_date,
            "Event Name": line,
            "Location and Address": location,
            "_Event URL": event_url,
        })

    if not events:
        raise RuntimeError(
            "The scraper found event links but could not match them "
            "to the date-grouped listing. No spreadsheet was created."
        )

    # Report any event links that weren't matched to listing text.
    unmatched = sum(len(q) for q in links_by_name.values())

    print(f"Matched {matched_links} event listings.")

    if unmatched:
        print(
            f"Warning: {unmatched} event links were not matched "
            "to a listing line."
        )

    # Remove only exact duplicate records, not events that share a name.
    df = pd.DataFrame(events)

    df = df.drop_duplicates(
        subset=[
            "Start Date",
            "End Date",
            "Event Name",
            "Location and Address",
            "_Event URL",
        ]
    )

    # Keep the requested four columns, in the requested order.
    df = df[
        [
            "Start Date",
            "End Date",
            "Event Name",
            "Location and Address",
        ]
    ]

    df = df.sort_values(
        by=["Start Date", "Event Name"],
        na_position="last",
        kind="stable",
    )

    excel_path = OUTPUT / "tcdb_texas_card_shows.xlsx"
    csv_path = OUTPUT / "tcdb_texas_card_shows.csv"
    debug_path = OUTPUT / "tcdb_debug.txt"

    df.to_excel(excel_path, index=False)
    df.to_csv(csv_path, index=False)

    # Save a simple audit report so missing matches can be investigated.
    with open(debug_path, "w", encoding="utf-8") as file:
        file.write(f"Source: {SOURCE_URL}\n")
        file.write(f"Event links found: {matched_links + unmatched}\n")
        file.write(f"Event listings matched: {matched_links}\n")
        file.write(f"Unmatched event links: {unmatched}\n")
        file.write(f"Rows exported: {len(df)}\n\n")
        file.write("Exported event listings:\n")

        for _, row in df.iterrows():
            file.write(
                f"{row['Start Date']} | "
                f"{row['Event Name']} | "
                f"{row['Location and Address']}\n"
            )

    print(f"Exported {len(df)} rows.")
    print(f"Excel file: {excel_path}")
    print(f"CSV file: {csv_path}")
    print(f"Audit report: {debug_path}")


if __name__ == "__main__":
    main()

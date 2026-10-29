import re
from collections import Counter, defaultdict, deque
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
OUTPUT.mkdir(parents=True, exist_ok=True)

SNAPSHOT_PATH = Path("data/previous_snapshot.csv")

COLUMNS = [
    "Start Date",
    "End Date",
    "Event Name",
    "Location and Address",
]

CHANGE_COLUMNS = ["Change Type"] + COLUMNS


# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------

def clean(text):
    """Normalize whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def normalize(text):
    """Normalize event names for matching."""
    return re.sub(
        r"[^a-z0-9]+", " ", clean(text).lower()
    ).strip()


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
    Collect individual event links from the listing.
    Preserve repeated event names in their original order.
    """
    links_by_name = defaultdict(deque)

    for anchor in soup.find_all("a", href=True):
        name = clean(anchor.get_text(" ", strip=True))
        url = urljoin(SOURCE_URL, anchor["href"])

        if not name or not is_event_link(url):
            continue

        params = parse_qs(urlparse(url).query)

        # Ignore links pointing back to the listing itself.
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

        location_parts.append(line)

        # City/state lines commonly look like "Austin, TX".
        if re.search(r",\s*[A-Z]{2}\b", line):
            break

    return ", ".join(location_parts)


def row_counter(dataframe):
    """
    Count event rows so identical duplicate rows are handled
    consistently when comparing snapshots.
    """
    return Counter(
        map(
            tuple,
            dataframe[COLUMNS].fillna("").astype(str).to_numpy().tolist(),
        )
    )


def create_change_report(current_df):
    """
    Compare the current successful scrape against the previous
    snapshot. Return a changes dataframe and the baseline status.
    """
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not SNAPSHOT_PATH.exists() or SNAPSHOT_PATH.stat().st_size == 0:
        print(
            "No previous snapshot found. "
            "This run will establish the initial baseline."
        )
        return pd.DataFrame(columns=CHANGE_COLUMNS), False

    previous_df = pd.read_csv(
        SNAPSHOT_PATH,
        dtype=str,
        keep_default_na=False,
    )

    if not all(column in previous_df.columns for column in COLUMNS):
        raise RuntimeError(
            "The previous snapshot is missing required columns. "
            "The snapshot will not be replaced."
        )

    previous_df = previous_df[COLUMNS].fillna("").astype(str)
    current_df = current_df[COLUMNS].fillna("").astype(str)

    previous_counts = row_counter(previous_df)
    current_counts = row_counter(current_df)

    changes = []

    # Events appearing more often now are new listings.
    for row, count in (current_counts - previous_counts).items():
        for _ in range(count):
            changes.append({
                "Change Type": "New",
                **dict(zip(COLUMNS, row)),
            })

    # Events appearing less often now are removed listings.
    for row, count in (previous_counts - current_counts).items():
        for _ in range(count):
            changes.append({
                "Change Type": "Removed",
                **dict(zip(COLUMNS, row)),
            })

    changes_df = pd.DataFrame(changes, columns=CHANGE_COLUMNS)

    if not changes_df.empty:
        changes_df = changes_df.sort_values(
            by=["Change Type", "Start Date", "Event Name"],
            kind="stable",
        ).reset_index(drop=True)

    return changes_df, True


# --------------------------------------------------
# MAIN SCRAPER
# --------------------------------------------------

def main():
    print("Loading the complete TCDB Texas listing...")

    soup = get_soup(SOURCE_URL)

    raw_lines = soup.get_text("\n", strip=True).splitlines()
    lines = [clean(line) for line in raw_lines if clean(line)]

    links_by_name = extract_event_links(soup)
    event_names = set(links_by_name.keys())

    if not event_names:
        raise RuntimeError(
            "No event links were found on the TCDB listing. "
            "The website may have changed or returned an incomplete page. "
            "No snapshot will be updated."
        )

    total_links = sum(len(queue) for queue in links_by_name.values())
    print(f"Found {total_links} event links.")

    events = []
    current_date = ""
    matched_links = 0

    for index, line in enumerate(lines):
        listing_date = parse_listing_date(line)

        if listing_date:
            current_date = listing_date
            continue

        normalized_name = normalize(line)

        if normalized_name not in links_by_name:
            continue

        if not links_by_name[normalized_name]:
            continue

        # Consume one matching link for each occurrence of an event name.
        links_by_name[normalized_name].popleft()
        matched_links += 1

        location = get_location(
            lines,
            index,
            event_names,
        )

        # TCDB groups shows by individual calendar date.
        # Use the listed date for both date columns.
        events.append({
            "Start Date": current_date,
            "End Date": current_date,
            "Event Name": line,
            "Location and Address": location,
        })

    if not events:
        raise RuntimeError(
            "The scraper found event links but could not match them "
            "to the date-grouped listing. No snapshot will be updated."
        )

    unmatched_links = sum(len(queue) for queue in links_by_name.values())

    print(f"Matched {matched_links} event listings.")

    if unmatched_links:
        print(
            f"Warning: {unmatched_links} event links did not match "
            "a listing line. Review the audit report."
        )

    # Remove exact duplicate records while retaining distinct events.
    df = pd.DataFrame(events, columns=COLUMNS)
    df = df.drop_duplicates(subset=COLUMNS)

    df = df.sort_values(
        by=["Start Date", "Event Name"],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)

    # Compare with the last successful snapshot before replacing it.
    changes_df, had_baseline = create_change_report(df)

    # --------------------------------------------------
    # EXPORT CURRENT LISTING AND CHANGE REPORT
    # --------------------------------------------------

    excel_path = OUTPUT / "tcdb_texas_card_shows.xlsx"
    csv_path = OUTPUT / "tcdb_texas_card_shows.csv"
    changes_excel_path = OUTPUT / "tcdb_daily_changes.xlsx"
    changes_csv_path = OUTPUT / "tcdb_daily_changes.csv"
    debug_path = OUTPUT / "tcdb_debug.txt"

    # Full current listing.
    df.to_excel(excel_path, index=False)
    df.to_csv(csv_path, index=False)

    # Only newly added and removed event listings.
    changes_df.to_excel(changes_excel_path, index=False)
    changes_df.to_csv(changes_csv_path, index=False)

    new_count = (
        int((changes_df["Change Type"] == "New").sum())
        if not changes_df.empty else 0
    )
    removed_count = (
        int((changes_df["Change Type"] == "Removed").sum())
        if not changes_df.empty else 0
    )

    # Write the audit report before updating the snapshot.
    with open(debug_path, "w", encoding="utf-8") as file:
        file.write(f"Source: {SOURCE_URL}\n")
        file.write(f"Event links found: {total_links}\n")
        file.write(f"Event listings matched: {matched_links}\n")
        file.write(f"Unmatched event links: {unmatched_links}\n")
        file.write(f"Rows exported: {len(df)}\n")
        file.write(f"Previous baseline existed: {had_baseline}\n")
        file.write(f"New listings: {new_count}\n")
        file.write(f"Removed listings: {removed_count}\n\n")
        file.write("Exported event listings:\n")

        for _, row in df.iterrows():
            file.write(
                f"{row['Start Date']} | "
                f"{row['End Date']} | "
                f"{row['Event Name']} | "
                f"{row['Location and Address']}\n"
            )

    # Update the baseline only after scraping and report exports succeed.
    # GitHub Actions must commit this file to preserve it between runs.
    df[COLUMNS].to_csv(SNAPSHOT_PATH, index=False)

    print(f"Exported {len(df)} current event rows.")
    print(f"New event listings: {new_count}")
    print(f"Removed event listings: {removed_count}")
    print(f"Full Excel listing: {excel_path}")
    print(f"Daily changes report: {changes_excel_path}")
    print(f"Audit report: {debug_path}")
    print(f"Comparison snapshot: {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()

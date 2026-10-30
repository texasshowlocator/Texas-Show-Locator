import csv
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


# ==================================================
# TEXAS SHOW LOCATOR - TCDB CSV SCRAPER
# ==================================================

TCDB_URL = (
    "https://www.tcdb.com/CardShows.cfm"
    "?MODE=Location&State=TX&Display=Texas&Country=United%20States"
)

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")

SNAPSHOT_FILE = DATA_DIR / "previous_snapshot.csv"
MAIN_CSV = OUTPUT_DIR / "texas_card_shows.csv"
CHANGES_CSV = OUTPUT_DIR / "daily_changes.csv"
AUDIT_FILE = OUTPUT_DIR / "scrape_audit.txt"

COLUMNS = [
    "Start Date",
    "End Date",
    "Event Name",
    "Event Location",
    "Source Website",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
    )
}

DATE_PATTERN = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"[A-Za-z]+ \d{1,2}, \d{4}",
    re.IGNORECASE,
)

CITY_PATTERN = re.compile(
    r"^[A-Za-z0-9 .,'’()&/-]+,\s*TX$",
    re.IGNORECASE,
)

TIME_PATTERN = re.compile(
    r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\s*-\s*"
    r"\d{1,2}:\d{2}\s*(?:AM|PM)\b",
    re.IGNORECASE,
)


# ==================================================
# TEXT AND DATE HELPERS
# ==================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_date(value):
    value = clean_text(value)

    try:
        return datetime.strptime(
            value, "%A, %B %d, %Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Could not parse date: {value}")


def event_key(event):
    """
    Compare events using start date, event name,
    and location.
    """
    return (
        clean_text(event["Start Date"]).casefold(),
        clean_text(event["Event Name"]).casefold(),
        clean_text(event["Event Location"]).casefold(),
    )


# ==================================================
# EVENT DATE EXTRACTION
# ==================================================

def get_event_date(anchor):
    """
    Find the nearest preceding date heading
    associated with the event listing.
    """
    previous_dates = anchor.find_all_previous(
        string=DATE_PATTERN
    )

    if not previous_dates:
        raise ValueError(
            f"No date heading found for "
            f"{anchor.get_text(' ', strip=True)}"
        )

    return parse_date(previous_dates[0])


# ==================================================
# EVENT LOCATION EXTRACTION
# ==================================================

def get_event_location(anchor):
    """
    Find the venue and Texas city in the event's
    surrounding listing block.
    """
    selected_lines = []

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

        has_city = any(
            CITY_PATTERN.match(line)
            for line in lines
        )

        has_time = any(
            TIME_PATTERN.search(line)
            for line in lines
        )

        if has_city and has_time:
            selected_lines = lines
            break

    if not selected_lines:
        raise ValueError(
            f"Could not identify location for "
            f"{anchor.get_text(' ', strip=True)}"
        )

    city_index = next(
        i for i, line in enumerate(selected_lines)
        if CITY_PATTERN.match(line)
    )

    city = selected_lines[city_index]
    venue = ""

    if city_index > 0:
        possible_venue = selected_lines[city_index - 1]

        if (
            possible_venue
            and not TIME_PATTERN.search(possible_venue)
            and not DATE_PATTERN.search(possible_venue)
        ):
            venue = possible_venue

    if venue:
        return f"{venue}, {city}"

    return city


# ==================================================
# TCDB SCRAPER
# ==================================================

def scrape_tcdb():

    print("Connecting to TCDB Texas Card Shows...")

    response = requests.get(
        TCDB_URL,
        headers=HEADERS,
        timeout=45,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    events = []
    seen = set()
    links_found = 0
    skipped = []

    for anchor in soup.find_all("a", href=True):

        href = anchor["href"]

        # TCDB event detail links contain CardShows.cfm
        # and a numeric ID parameter.
        if "CardShows.cfm" not in href:
            continue

        if not re.search(
            r"(?:\?|&)ID=\d+",
            href,
            re.IGNORECASE,
        ):
            continue

        event_name = clean_text(
            anchor.get_text(" ", strip=True)
        )

        if not event_name:
            continue

        links_found += 1

        try:

            start_date = get_event_date(anchor)
            location = get_event_location(anchor)

            event = {
                "Start Date": start_date,
                "End Date": start_date,
                "Event Name": event_name,
                "Event Location": location,
                "Source Website": TCDB_URL,
            }

            key = event_key(event)

            if key not in seen:
                events.append(event)
                seen.add(key)

        except Exception as exc:

            skipped.append(
                f"{event_name}: {exc}"
            )

    if links_found == 0:
        raise RuntimeError(
            "No recognizable TCDB event links found. "
            "The website layout may have changed."
        )

    if not events:
        raise RuntimeError(
            "TCDB event links were found, but no events "
            "could be parsed."
        )

    events.sort(
        key=lambda item: (
            item["Start Date"],
            item["Event Name"].casefold(),
            item["Event Location"].casefold(),
        )
    )

    print(f"Event links found: {links_found}")
    print(f"Events collected: {len(events)}")
    print(f"Events skipped: {len(skipped)}")

    return events, links_found, skipped


# ==================================================
# CSV WRITER
# ==================================================

def write_csv(path, rows, columns=COLUMNS):

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


# ==================================================
# SNAPSHOT READER
# ==================================================

def read_snapshot():

    if not SNAPSHOT_FILE.exists():
        return []

    with SNAPSHOT_FILE.open(
        "r",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        reader = csv.DictReader(file)

        return [
            {
                column: row.get(column, "")
                for column in COLUMNS
            }
            for row in reader
        ]


# ==================================================
# DAILY CHANGE COMPARISON
# ==================================================

def calculate_changes(
    current_events,
    previous_events,
):

    previous_by_key = {
        event_key(event): event
        for event in previous_events
    }

    current_by_key = {
        event_key(event): event
        for event in current_events
    }

    new_keys = (
        current_by_key.keys()
        - previous_by_key.keys()
    )

    removed_keys = (
        previous_by_key.keys()
        - current_by_key.keys()
    )

    changes = []

    for key in sorted(new_keys):

        event = current_by_key[key].copy()
        event["Change"] = "NEW"

        changes.append(event)

    for key in sorted(removed_keys):

        event = previous_by_key[key].copy()
        event["Change"] = "REMOVED"

        changes.append(event)

    return changes


# ==================================================
# DAILY CHANGES CSV
# ==================================================

def write_changes_csv(changes):

    change_columns = ["Change"] + COLUMNS

    write_csv(
        CHANGES_CSV,
        changes,
        change_columns,
    )


# ==================================================
# AUDIT REPORT
# ==================================================

def write_audit(
    status,
    event_count=0,
    links_found=0,
    skipped=None,
    error=None,
    new_count=0,
    removed_count=0,
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

        file.write("=" * 45 + "\n")

        file.write(
            f"Run time: "
            f"{datetime.now().isoformat(timespec='seconds')}\n"
        )

        file.write("Source: TCDB Texas Card Shows\n")
        file.write(f"Status: {status}\n")
        file.write(f"Event links found: {links_found}\n")
        file.write(f"Events collected: {event_count}\n")
        file.write(f"New events: {new_count}\n")
        file.write(f"Removed events: {removed_count}\n")

        if error:
            file.write(f"\nERROR:\n{error}\n")

        if skipped:

            file.write(
                "\nEVENTS THAT COULD NOT BE PARSED:\n"
            )

            for item in skipped:
                file.write(f"- {item}\n")


# ==================================================
# MAIN PROGRAM
# ==================================================

def main():

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ----------------------------------------------
    # 1. Scrape TCDB
    # ----------------------------------------------

    try:

        (
            current_events,
            links_found,
            skipped,
        ) = scrape_tcdb()

    except Exception as exc:

        message = str(exc)

        print(f"\nSCRAPE FAILED: {message}")
        print("Previous snapshot was preserved.")

        write_audit(
            status="FAILED - PREVIOUS SNAPSHOT PRESERVED",
            error=message,
        )

        sys.exit(1)

    # ----------------------------------------------
    # 2. Reject incomplete scrapes
    # ----------------------------------------------

    if skipped:

        message = (
            f"{len(skipped)} event listings could not "
            "be parsed. Snapshot was not updated."
        )

        print(f"\nSCRAPE FAILED: {message}")
        print("Previous snapshot was preserved.")

        write_audit(
            status="FAILED - PREVIOUS SNAPSHOT PRESERVED",
            event_count=len(current_events),
            links_found=links_found,
            skipped=skipped,
            error=message,
        )

        sys.exit(1)

    # ----------------------------------------------
    # 3. Read previous snapshot
    # ----------------------------------------------

    previous_events = read_snapshot()

    # ----------------------------------------------
    # 4. Compare current and previous events
    # ----------------------------------------------

    changes = calculate_changes(
        current_events,
        previous_events,
    )

    new_count = sum(
        1 for row in changes
        if row["Change"] == "NEW"
    )

    removed_count = sum(
        1 for row in changes
        if row["Change"] == "REMOVED"
    )

    # ----------------------------------------------
    # 5. Write main CSV
    # ----------------------------------------------

    write_csv(
        MAIN_CSV,
        current_events,
    )

    # ----------------------------------------------
    # 6. Write daily changes CSV
    # ----------------------------------------------

    write_changes_csv(changes)

    # ----------------------------------------------
    # 7. Update snapshot after successful scrape
    # ----------------------------------------------

    write_csv(
        SNAPSHOT_FILE,
        current_events,
    )

    # ----------------------------------------------
    # 8. Write audit report
    # ----------------------------------------------

    write_audit(
        status="SUCCESS",
        event_count=len(current_events),
        links_found=links_found,
        skipped=skipped,
        new_count=new_count,
        removed_count=removed_count,
    )

    # ----------------------------------------------
    # 9. Print results
    # ----------------------------------------------

    print("\n===================================")
    print("TEXAS SHOW LOCATOR SCRAPE COMPLETE")
    print("===================================")

    print(f"Events collected: {len(current_events)}")
    print(f"New events: {new_count}")
    print(f"Removed events: {removed_count}")

    print("\nFILES CREATED:")

    print(f"- {MAIN_CSV}")
    print(f"- {CHANGES_CSV}")
    print(f"- {AUDIT_FILE}")
    print(f"- {SNAPSHOT_FILE}")


if __name__ == "__main__":
    main()

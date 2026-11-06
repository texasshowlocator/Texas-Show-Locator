import csv
import re
import sys
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path("output")
TCDB_FILE = OUTPUT_DIR / "texas_card_shows.csv"
ONTREASURE_FILE = OUTPUT_DIR / "ontreasure_events.csv"
COINZIP_FILE = OUTPUT_DIR / "coinzip_events.csv"
COMBINED_FILE = OUTPUT_DIR / "combined_texas_card_shows.csv"

BASE_COLUMNS = ["Start", "End", "Event", "Venue/Address"]
OUTPUT_COLUMNS = BASE_COLUMNS + ["Source"]
SOURCE_ORDER = ("TCDB", "OnTreasure", "CoinZip")

CHANGE_FILES = (
    (OUTPUT_DIR / "daily_changes.csv", "TCDB"),
    (OUTPUT_DIR / "ontreasure_daily_changes.csv", "OnTreasure"),
    (OUTPUT_DIR / "coinzip_daily_changes.csv", "CoinZip"),
)
COMBINED_CHANGES_FILE = OUTPUT_DIR / "combined_daily_changes.csv"
CHANGE_OUTPUT_COLUMNS = ["Change"] + BASE_COLUMNS + ["Source"]


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_source(path, source_name):
    if not path.exists():
        raise FileNotFoundError(
            f"{path} was not created. The {source_name} scraper may have failed."
        )

    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = [column for column in BASE_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")

        rows = []
        for line_number, row in enumerate(reader, start=2):
            event = {column: clean(row.get(column)) for column in BASE_COLUMNS}
            if not event["Start"] or not event["End"] or not event["Event"]:
                raise ValueError(f"{path}, row {line_number}: Start, End, and Event are required.")
            datetime.strptime(event["Start"], "%m/%d/%y")
            datetime.strptime(event["End"], "%m/%d/%y")
            event["Source"] = source_name
            rows.append(event)

        if not rows:
            raise ValueError(f"{path} contains no event records.")
        return rows


def event_key(event):
    return tuple(clean(event[column]).casefold() for column in ("Start", "Event", "Venue/Address"))


def merge_sources(*sources):
    combined = {}
    for events in sources:
        for event in events:
            key = event_key(event)
            if key not in combined:
                combined[key] = event.copy()
                continue

            existing = combined[key]
            if datetime.strptime(event["End"], "%m/%d/%y") > datetime.strptime(existing["End"], "%m/%d/%y"):
                existing["End"] = event["End"]
            source_names = set(existing["Source"].split("; "))
            source_names.add(event["Source"])
            existing["Source"] = "; ".join(name for name in SOURCE_ORDER if name in source_names)

    return sorted(combined.values(), key=lambda row: (
        datetime.strptime(row["Start"], "%m/%d/%y"),
        row["Event"].casefold(),
        row["Venue/Address"].casefold(),
    ))



def read_changes(path, source_name):
    if not path.exists():
        raise FileNotFoundError(
            f"{path} was not created. The {source_name} scraper may have failed."
        )

    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required = ["Change"] + BASE_COLUMNS
        missing = [column for column in required if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")

        rows = []
        for line_number, row in enumerate(reader, start=2):
            event = {column: clean(row.get(column)) for column in required}
            if not event["Change"] or not event["Start"] or not event["End"] or not event["Event"]:
                raise ValueError(
                    f"{path}, row {line_number}: Change, Start, End, and Event are required."
                )
            datetime.strptime(event["Start"], "%m/%d/%y")
            datetime.strptime(event["End"], "%m/%d/%y")
            event["Source"] = source_name
            rows.append(event)
        return rows


def combine_daily_changes():
    combined = {}
    source_counts = {}

    for path, source_name in CHANGE_FILES:
        events = read_changes(path, source_name)
        source_counts[source_name] = len(events)

        for event in events:
            key = (
                event["Change"].casefold(),
                event["Start"].casefold(),
                event["Event"].casefold(),
                event["Venue/Address"].casefold(),
            )
            if key not in combined:
                combined[key] = event.copy()
                continue

            existing = combined[key]
            source_names = set(existing["Source"].split("; "))
            source_names.add(source_name)
            existing["Source"] = "; ".join(
                name for name in SOURCE_ORDER if name in source_names
            )
            if datetime.strptime(event["End"], "%m/%d/%y") > datetime.strptime(existing["End"], "%m/%d/%y"):
                existing["End"] = event["End"]

    rows = sorted(combined.values(), key=lambda row: (
        datetime.strptime(row["Start"], "%m/%d/%y"),
        row["Event"].casefold(),
        row["Change"].casefold(),
        row["Venue/Address"].casefold(),
    ))

    with COMBINED_CHANGES_FILE.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CHANGE_OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print("\\nCOMBINED DAILY CHANGES CREATED")
    for source_name in SOURCE_ORDER:
        print(f"{source_name} changes: {source_counts.get(source_name, 0)}")
    print(f"Combined change rows: {len(rows)}")
    print(f"Output: {COMBINED_CHANGES_FILE}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tcdb_events = read_source(TCDB_FILE, "TCDB")
    ontreasure_events = read_source(ONTREASURE_FILE, "OnTreasure")
    coinzip_events = read_source(COINZIP_FILE, "CoinZip")
    combined = merge_sources(tcdb_events, ontreasure_events, coinzip_events)
    combine_daily_changes()

    with COMBINED_FILE.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(combined)

    print("COMBINED TEXAS SHOW REPORT CREATED")
    print(f"TCDB events: {len(tcdb_events)}")
    print(f"OnTreasure events: {len(ontreasure_events)}")
    print(f"CoinZip events: {len(coinzip_events)}")
    print(f"Combined unique events: {len(combined)}")
    print(f"Output: {COMBINED_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"COMBINED REPORT FAILED: {exc}")
        sys.exit(1)

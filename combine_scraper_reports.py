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
COMBINED_AUDIT_FILE = OUTPUT_DIR / "combined_report_audit.txt"

BASE_COLUMNS = ["Start", "End", "Event", "Venue/Address"]
OUTPUT_COLUMNS = BASE_COLUMNS + ["Source"]
SOURCE_ORDER = ("TCDB", "OnTreasure", "CoinZip")

SOURCES = {
    "TCDB": {
        "events": TCDB_FILE,
        "changes": OUTPUT_DIR / "daily_changes.csv",
        "audit": OUTPUT_DIR / "scrape_audit.txt",
    },
    "OnTreasure": {
        "events": ONTREASURE_FILE,
        "changes": OUTPUT_DIR / "ontreasure_daily_changes.csv",
        "audit": OUTPUT_DIR / "ontreasure_scrape_audit.txt",
    },
    "CoinZip": {
        "events": COINZIP_FILE,
        "changes": OUTPUT_DIR / "coinzip_daily_changes.csv",
        "audit": OUTPUT_DIR / "coinzip_scrape_audit.txt",
    },
}
COMBINED_CHANGES_FILE = OUTPUT_DIR / "combined_daily_changes.csv"
CHANGE_OUTPUT_COLUMNS = ["Change"] + BASE_COLUMNS + ["Source"]


def clean(value):
    return re.sub(r"\\s+", " ", str(value or "")).strip()


def read_source_status(source_name):
    audit_path = SOURCES[source_name]["audit"]
    if not audit_path.exists():
        return False, "Audit file missing"

    content = audit_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^Status:\\s*(.+?)\\s*$", content, re.I | re.M)
    if not match:
        return False, "Status line missing from audit"
    status = match.group(1).strip()
    return status.upper() == "SUCCESS", status


def read_source(path, source_name):
    if not path.exists():
        raise FileNotFoundError(
            f"{path} was not created although {source_name} reported success."
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
            raise ValueError(f"{path} contains no event records despite {source_name} reporting success.")
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
            f"{path} was not created although {source_name} reported success."
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


def combine_daily_changes(active_sources):
    combined = {}
    source_counts = {}

    for source_name in active_sources:
        path = SOURCES[source_name]["changes"]
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
    for source_name in active_sources:
        print(f"{source_name} changes: {source_counts.get(source_name, 0)}")
    print(f"Combined change rows: {len(rows)}")
    print(f"Output: {COMBINED_CHANGES_FILE}")


def write_combined_audit(active_sources, failed_sources, event_counts):
    partial = bool(failed_sources)
    status = "PARTIAL - ONE OR MORE SOURCES FAILED" if partial else "SUCCESS"
    with COMBINED_AUDIT_FILE.open("w", encoding="utf-8") as file:
        file.write("TEXAS SHOW LOCATOR - COMBINED REPORT AUDIT\\n")
        file.write("=" * 55 + "\\n")
        file.write(f"Run time: {datetime.now().isoformat(timespec='seconds')}\\n")
        file.write(f"Status: {status}\\n")
        file.write("Included sources: " + (", ".join(active_sources) or "None") + "\\n")
        file.write("Excluded sources: " + (", ".join(failed_sources) or "None") + "\\n")
        file.write("\\nEVENT COUNTS\\n")
        for source_name in active_sources:
            file.write(f"{source_name}: {event_counts[source_name]}\\n")
        file.write(f"Combined unique events: {sum(event_counts.values()) if len(active_sources) == 1 else 'see combined CSV'}\\n")
        if partial:
            file.write(
                "\\nWARNING: This is a partial report. Failed sources were excluded; "
                "their previous master CSVs and daily changes were not reused.\\n"
            )


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    active_sources = []
    failed_sources = []
    statuses = {}

    for source_name in SOURCE_ORDER:
        success, status = read_source_status(source_name)
        statuses[source_name] = status
        if success:
            active_sources.append(source_name)
        else:
            failed_sources.append(f"{source_name} ({status})")

    if not active_sources:
        raise RuntimeError(
            "No source scraper completed successfully. No combined reports were generated."
        )

    source_rows = []
    event_counts = {}
    for source_name in active_sources:
        rows = read_source(SOURCES[source_name]["events"], source_name)
        source_rows.append(rows)
        event_counts[source_name] = len(rows)

    combined = merge_sources(*source_rows)
    combine_daily_changes(active_sources)

    with COMBINED_FILE.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(combined)

    write_combined_audit(active_sources, failed_sources, event_counts)

    print("COMBINED TEXAS SHOW REPORT CREATED")
    for source_name in active_sources:
        print(f"{source_name} events: {event_counts[source_name]}")
    print(f"Combined unique events: {len(combined)}")
    print(f"Report status: {'PARTIAL' if failed_sources else 'SUCCESS'}")
    print(f"Excluded sources: {', '.join(failed_sources) if failed_sources else 'None'}")
    print(f"Output: {COMBINED_FILE}")
    print(f"Audit: {COMBINED_AUDIT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"COMBINED REPORT FAILED: {exc}")
        sys.exit(1)

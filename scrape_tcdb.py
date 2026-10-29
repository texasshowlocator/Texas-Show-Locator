import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

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

DATE_RE = re.compile(
    r"^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*)?"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})$",
    re.IGNORECASE,
)
CITY_RE = re.compile(r"^(.+?),\s*TX$", re.IGNORECASE)
TIME_RE = re.compile(
    r"^\d{1,2}(?::\d{2})?\s*[AP]M\s*-\s*"
    r"\d{1,2}(?::\d{2})?\s*[AP]M$",
    re.IGNORECASE,
)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_date(line):
    match = DATE_RE.match(clean(line))
    if not match:
        return None

    try:
        return datetime.strptime(
            match.group(1), "%B %d, %Y"
        ).date()
    except ValueError:
        return None


def main():
    response = requests.get(
        SOURCE_URL, headers=HEADERS, timeout=40
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Preserve the page's displayed order.
    lines = [
        clean(line)
        for line in soup.get_text("\n").splitlines()
        if clean(line)
    ]

    # Map visible event names to their actual links.
    event_links = {}

    for a in soup.find_all("a", href=True):
        name = clean(a.get_text(" ", strip=True))
        href = a["href"]

        if not name:
            continue

        full_url = urljoin(SOURCE_URL, href)

        # Exclude external navigation and unrelated page links.
        if "tcdb.com/CardShows.cfm" in full_url:
            event_links.setdefault(name.casefold(), full_url)

    events = []
    current_date = None

    for i, line in enumerate(lines):
        found_date = parse_date(line)

        if found_date:
            current_date = found_date
            continue

        if not current_date:
            continue

        # An event listing starts with a linked event name.
        event_url = event_links.get(line.casefold())

        if not event_url:
            continue

        # Read ahead until the next event link or date heading.
        details = []

        for candidate in lines[i + 1:i + 6]:
            if parse_date(candidate):
                break

            # Stop if the next event name appears.
            if candidate.casefold() in event_links:
                break

            details.append(candidate)

        venue = ""
        city = ""
        time = ""

        for detail in details:
            city_match = CITY_RE.match(detail)

            if city_match:
                city = clean(city_match.group(1))
            elif TIME_RE.match(detail):
                time = detail
            elif not venue:
                venue = detail

        review = []

        if not venue:
            review.append("Venue not detected")
        if not city:
            review.append("City not detected")
        if not time:
            review.append("Time not detected")

        events.append({
            "Start Date": current_date,
            "End Date": "",
            "Event Name": line,
            "Venue": venue,
            "City": city,
            "State": "TX",
            "Time": time,
            "Source Website": "TCDB",
            "Event URL": event_url,
            "Review Needed": "; ".join(review),
        })

    if not events:
        # Save page text to help diagnose an unexpected layout.
        (OUTPUT / "tcdb_debug.txt").write_text(
            "\n".join(lines), encoding="utf-8"
        )
        raise RuntimeError(
            "No events found. Check output/tcdb_debug.txt "
            "in the workflow artifacts or logs."
        )

    df = pd.DataFrame(events)

    # Remove exact duplicate records, preserving separate dates,
    # venues, and times for multi-day events.
    df = df.drop_duplicates(
        subset=[
            "Start Date", "Event Name", "Venue",
            "City", "Time", "Event URL"
        ]
    )

    df = df.sort_values(
        ["Start Date", "City", "Event Name"],
        na_position="last",
    ).reset_index(drop=True)

    df.to_csv(
        OUTPUT / "texas_card_shows.csv",
        index=False,
        encoding="utf-8-sig",
    )

    with pd.ExcelWriter(
        OUTPUT / "texas_card_shows.xlsx",
        engine="openpyxl",
        date_format="mm/dd/yyyy",
    ) as writer:
        df.to_excel(writer, index=False, sheet_name="Texas Shows")
        ws = writer.sheets["Texas Shows"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        for cells in ws.columns:
            width = max(
                len(str(cell.value or "")) for cell in cells
            )
            ws.column_dimensions[
                cells[0].column_letter
            ].width = min(width + 2, 55)

    print(f"Source URL: {SOURCE_URL}")
    print(f"Unique event records extracted: {len(df)}")
    print(
        "Records needing review: "
        f"{(df['Review Needed'] != '').sum()}"
    )
    print("Output files created:")
    print(" - output/texas_card_shows.csv")
    print(" - output/texas_card_shows.xlsx")


if __name__ == "__main__":
    main()

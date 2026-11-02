import csv
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# TEXAS SHOW LOCATOR - COINZIP SCRAPER
BASE_URL = "https://coinzip.com"
LISTING_URL = f"{BASE_URL}/Browse/C160534-C161363/US-Coin-Shows-Texas"
DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
SNAPSHOT_FILE = DATA_DIR / "coinzip_previous_snapshot.csv"
MAIN_CSV = OUTPUT_DIR / "coinzip_events.csv"
CHANGES_CSV = OUTPUT_DIR / "coinzip_daily_changes.csv"
AUDIT_FILE = OUTPUT_DIR / "coinzip_scrape_audit.txt"
COLUMNS = ["Start", "End", "Event", "Venue/Address"]
CHANGE_COLUMNS = ["Change"] + COLUMNS
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TexasShowLocator/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}
MONTHS = {name.lower(): number for number, name in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}
MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})
MONTH_PATTERN = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_RE = re.compile(
    rf"(?:(?P<month>{MONTH_PATTERN})\.?\s+)?"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*(?:-|–|—|&|and)\s*(?:(?P<month2>{MONTH_PATTERN})\.?\s+)?"
    rf"(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"\s*,?\s*(?P<year>20\d{{2}})?",
    re.I,
)
MONTH_DATE_RE = re.compile(
    rf"(?P<month>{MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*(?:-|–|—|&|and)\s*(?:(?P<month2>{MONTH_PATTERN})\.?\s+)?"
    rf"(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"\s*,?\s*(?P<year>20\d{{2}})?",
    re.I,
)


def clean(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"\s+(?:Add to Watch List|Watching|View Watchlist\s*>?).*$", "", value, flags=re.I)
    value = re.sub(r"\s+View\s*$", "", value, flags=re.I)
    return value.strip(" |•-")


def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=5, connect=5, read=5, status=5, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def parse_dates(text):
    text = clean(text)
    matches = list(MONTH_DATE_RE.finditer(text))
    if not matches:
        return None
    explicit_years = [int(m.group("year")) for m in matches if m.group("year")]
    if not explicit_years:
        year_match = re.search(r"\b(20\d{2})\b", text)
        explicit_years = [int(year_match.group(1))] if year_match else []
    default_year = explicit_years[0] if explicit_years else date.today().year

    def get_date(match, end=False):
        month_name = match.group("month2") if end and match.group("month2") else match.group("month")
        day_text = match.group("day2") if end and match.group("day2") else match.group("day")
        if not month_name or not day_text:
            return None
        year = int(match.group("year")) if match.group("year") else default_year
        try:
            return date(year, MONTHS[month_name.lower().rstrip(".")], int(day_text))
        except ValueError:
            return None

    first = get_date(matches[0])
    last_match = matches[-1]
    last = get_date(last_match, end=True)
    if first is None:
        return None
    if last is None:
        last = first
    # If a range crosses December into January, move the end into the next year.
    if last < first and last.month < first.month:
        last = last.replace(year=last.year + 1)
    return first, last


def discover_listings(session):
    pending = [LISTING_URL]
    visited_pages = set()
    listings = {}
    while pending:
        page_url = pending.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        response = session.get(page_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select('a[href*="/Listing/Details/"]'):
            href = urljoin(BASE_URL, anchor.get("href", ""))
            title = clean(anchor.get_text(" ", strip=True))
            if not title or title.lower() in {"view", "image"}:
                continue
            parent_text = ""
            parent = anchor
            for _ in range(4):
                if parent.parent is None:
                    break
                parent = parent.parent
                parent_text = clean(parent.get_text(" ", strip=True))
                if len(parent_text) > len(title) + 10:
                    break
            listings.setdefault(href, {"url": href, "title": title, "listing_text": parent_text})
        # Follow only pagination links on the same CoinZip Texas browse route.
        for anchor in soup.select("a[href]"):
            href = urljoin(BASE_URL, anchor.get("href", ""))
            parsed = urlparse(href)
            if parsed.netloc == "coinzip.com" and parsed.path.rstrip("/") == urlparse(LISTING_URL).path.rstrip("/") and href not in visited_pages:
                if re.search(r"(page|paged|p)=\d+", parsed.query, re.I):
                    pending.append(href)
        time.sleep(0.2)
    return list(listings.values())


def extract_address(lines):
    """Return only event-location information found in the event description."""
    lines = [clean(line) for line in lines if clean(line)]
    description_index = next(
        (i for i, line in enumerate(lines) if line.casefold() == "description"),
        None,
    )
    if description_index is None:
        return ""

    description = lines[description_index + 1:]
    stop = next(
        (i for i, line in enumerate(description)
         if re.match(r"^(questions\s*&\s*answers|post a question|similar listings)\b", line, re.I)),
        len(description),
    )
    description = description[:stop]
    text = " ".join(description)

    # Prefer a venue/address phrase in the event description.
    patterns = [
        r"(?:conveniently located|located|held|taking place)\s+at\s+(.+?\b(?:TX|Texas)\b(?:\s+\d{5}(?:-\d{4})?)?)",
        r"\bat\s+((?:The\s+)?[A-Z][^.;]{5,140}?\b(?:TX|Texas)\b(?:\s+\d{5}(?:-\d{4})?)?)",
        r"((?:[A-Z][A-Za-z0-9.'& -]{2,70}),\s*\d{1,6}\s+[A-Za-z0-9 .'-]+,\s*[A-Z][A-Za-z .'-]+,\s*TX(?:\s+\d{5})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            result = clean(match.group(1)).strip(" ,.;")
            if not re.search(r"\b(?:seller|contact|email|watchlist|zip search|browse)\b", result, re.I):
                return result

    # Some listings give a venue and street address but omit the state.
    for index, line in enumerate(description):
        if re.search(r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Highway|Hwy)\b", line, re.I):
            return clean(" ".join(description[max(0, index - 1):index + 2]))
        if re.search(r"^\d{1,6}\s+[A-Za-z0-9 .'-]+,\s*[A-Z][A-Za-z .'-]+$", line):
            venue = description[index - 1] if index > 0 else ""
            city = ""
            for candidate in description[index + 1:index + 3]:
                if re.search(r"\b(?:TX|Texas)\b", candidate, re.I):
                    city = candidate
                    break
            return clean(", ".join(part for part in (venue, line, city) if part))

    return ""


def scrape_event(session, listing):
    response = session.get(listing["url"], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title_node = soup.find("h1")
    if title_node:
        title_copy = BeautifulSoup(str(title_node), "html.parser")
        for node in title_copy.select("a, button, svg, img"):
            node.decompose()
        title = clean(title_copy.get_text(" ", strip=True))
    else:
        title = clean(listing.get("title", ""))

    # Some CoinZip pages accidentally expose description text in the heading.
    for marker in (
        " Over 125 dealers", " Sponsored by", " The show is", " This show ",
        " FREE admission", " Public Hours:", " Dealer set up"
    ):
        if marker.casefold() in title.casefold():
            title = re.split(re.escape(marker), title, maxsplit=1, flags=re.I)[0].strip()

    lines = [clean(line) for line in soup.get_text("\n").splitlines() if clean(line)]
    description_index = next(
        (i for i, line in enumerate(lines) if line.casefold() == "description"),
        None,
    )
    description_lines = lines[description_index + 1:] if description_index is not None else []
    description_stop = next(
        (i for i, line in enumerate(description_lines)
         if re.match(r"^(questions\s*&\s*answers|post a question|similar listings)\b", line, re.I)),
        len(description_lines),
    )
    description_lines = description_lines[:description_stop]
    description_text = " ".join(description_lines)

    address = extract_address(lines)

    # Prefer public event dates in the description over setup dates in the title.
    public_date_text = description_text
    setup_match = re.search(
        r"\b(?:dealer\s+set[\s-]?up|dealer\s+setup|early\s+bird)\b",
        public_date_text, re.I
    )
    if setup_match:
        public_date_text = public_date_text[:setup_match.start()]

    dates = parse_dates(public_date_text) if public_date_text else None
    if dates is None:
        date_parts = [
            clean(listing.get("title", "")),
            title,
            clean(listing.get("listing_text", "")),
        ]
        dates = parse_dates(" ".join(date_parts))
    if dates is None:
        raise ValueError(f"Could not parse event dates for {title!r}")

    start, end = dates
    if end < date.today():
        return None

    if not title or title.casefold() in {"view", "image"}:
        raise ValueError("Event title was empty after cleaning.")
    if re.search(r"add to watch list|view watchlist|watching|zip search|browse", title, re.I):
        raise ValueError(f"Event title contains CoinZip page text: {title!r}")

    # Never use seller/profile information as the event venue.
    if re.search(r"\b(?:seller|contact|watchlist|zip search|browse)\b", address, re.I):
        address = ""

    return {
        "Start": start.strftime("%m/%d/%y"),
        "End": end.strftime("%m/%d/%y"),
        "Event": title,
        "Venue/Address": address,
    }


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_snapshot():
    if not SNAPSHOT_FILE.exists():
        return [], False
    with SNAPSHOT_FILE.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != COLUMNS:
            return [], False
        return [{key: row.get(key, "") for key in COLUMNS} for row in reader], True


def event_name_key(row):
    return re.sub(r"[^a-z0-9]+", "", clean(row.get("Event", "")).casefold())


def date_range(row):
    return (datetime.strptime(row["Start"], "%m/%d/%y").date(),
            datetime.strptime(row["End"], "%m/%d/%y").date())


def date_gap_days(left, right):
    left_start, left_end = date_range(left)
    right_start, right_end = date_range(right)
    if left_start <= right_end and right_start <= left_end:
        return 0
    if left_end < right_start:
        return (right_start - left_end).days
    return (left_start - right_end).days


def reconcile_events(previous, current, date_tolerance_days=45):
    """Match by normalized title and nearest date; venue is secondary."""
    unmatched_old = set(range(len(previous)))
    reconciled, changes = [], []

    for new_row in sorted(current, key=lambda row: (row["Start"], row["Event"].casefold())):
        candidates = [
            (date_gap_days(previous[i], new_row), i)
            for i in unmatched_old
            if event_name_key(previous[i]) == event_name_key(new_row)
        ]
        candidates = [item for item in candidates if item[0] <= date_tolerance_days]
        if not candidates:
            row = new_row.copy()
            reconciled.append(row)
            change = row.copy()
            change["Change"] = "NEW"
            changes.append(change)
            continue

        _, old_index = min(candidates, key=lambda item: (item[0], previous[item[1]]["Start"]))
        unmatched_old.remove(old_index)
        old_row = previous[old_index]
        row = new_row.copy()

        # Do not erase a previously confirmed venue when this scrape returns blank.
        if not clean(row.get("Venue/Address")) and clean(old_row.get("Venue/Address")):
            row["Venue/Address"] = old_row["Venue/Address"]

        reconciled.append(row)
        changed = any(clean(old_row.get(field)) != clean(row.get(field))
                      for field in ("Start", "End", "Venue/Address"))
        if changed:
            change = row.copy()
            change["Change"] = "UPDATED"
            changes.append(change)

    for index in unmatched_old:
        row = previous[index].copy()
        row["Change"] = "REMOVED"
        changes.append(row)

    reconciled.sort(key=lambda row: (datetime.strptime(row["Start"], "%m/%d/%y"), row["Event"].casefold()))
    changes.sort(key=lambda row: (datetime.strptime(row["Start"], "%m/%d/%y"), row["Event"].casefold(), row["Change"]))
    return reconciled, changes


def main():
    started = datetime.now().isoformat(timespec="seconds")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    status, error = "SUCCESS", ""
    events, failures, found = [], [], 0
    try:
        session = create_session()
        listings = discover_listings(session)
        found = len(listings)
        if not listings:
            raise RuntimeError("CoinZip returned no Texas show listings.")
        for listing in listings:
            try:
                event = scrape_event(session, listing)
                if event:
                    events.append(event)
            except Exception as exc:
                failures.append(f"{listing['title']}: {exc}")
            time.sleep(0.5)
        if failures:
            raise RuntimeError(f"{len(failures)} of {found} listings could not be processed.")
        events.sort(key=lambda row: (datetime.strptime(row["Start"], "%m/%d/%y"), row["Event"].casefold()))
        if not events:
            raise RuntimeError("No upcoming Texas events were collected.")
        previous, has_snapshot = read_snapshot()
        changes = []
        if has_snapshot:
            events, changes = reconcile_events(previous, events)
        write_csv(MAIN_CSV, events, COLUMNS)
        write_csv(CHANGES_CSV, changes, CHANGE_COLUMNS)
        temp = SNAPSHOT_FILE.with_suffix(".tmp")
        write_csv(temp, events, COLUMNS)
        temp.replace(SNAPSHOT_FILE)
        baseline = not has_snapshot
    except Exception as exc:
        status, error, baseline = "FAILED - SNAPSHOT PRESERVED", str(exc), False
        changes = []
    with AUDIT_FILE.open("w", encoding="utf-8") as file:
        file.write("TEXAS SHOW LOCATOR - COINZIP SCRAPE AUDIT\n")
        file.write("=" * 52 + "\n")
        file.write(f"Run time: {started}\nSource: {LISTING_URL}\nStatus: {status}\n")
        file.write(f"Event links found: {found}\nEvents collected: {len(events)}\n")
        file.write(f"New events: {sum(row['Change'] == 'NEW' for row in changes)}\n")
        file.write(f"Updated events: {sum(row['Change'] == 'UPDATED' for row in changes)}\n")
        file.write(f"Removed events: {sum(row['Change'] == 'REMOVED' for row in changes)}\n")
        if baseline:
            file.write("A new snapshot baseline was created; no changes were reported.\n")
        if error:
            file.write(f"\nERROR: {error}\n")
        for failure in failures:
            file.write(f"- {failure}\n")
    print(f"CoinZip scrape: {status}; {len(events)} upcoming Texas events from {found} listings.")
    if status != "SUCCESS":
        sys.exit(1)


if __name__ == "__main__":
    main()

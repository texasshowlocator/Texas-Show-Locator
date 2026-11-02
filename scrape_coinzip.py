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
MONTH_PATTERN = "|".join(MONTHS)
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
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
    lines = [clean(line) for line in lines if clean(line)]
    # Prefer the block after a Location label; stop at common next sections.
    for index, line in enumerate(lines):
        if re.match(r"^location\s*:?$", line, re.I):
            block = []
            for candidate in lines[index + 1:index + 8]:
                if re.match(r"^(hours|admission|zip code|questions|description)\b", candidate, re.I):
                    break
                block.append(candidate)
                if re.search(r"\bTX\s+\d{5}(?:-\d{4})?\b", candidate, re.I):
                    break
            text = ", ".join(block)
            if text:
                return text
    for index, line in enumerate(lines):
        if re.search(r"\b(?:TX|Texas)\b", line, re.I):
            return ", ".join(lines[max(0, index - 2):index + 1])
    return ""


def scrape_event(session, listing):
    response = session.get(listing["url"], timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    lines = [clean(line) for line in soup.get_text("\n").splitlines() if clean(line)]
    title_node = soup.find("h1")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else listing["title"]
    if not title:
        title = listing["title"]

    # The category is already Texas-specific; still validate the detail page's
    # location when a state abbreviation is exposed.
    state_evidence = " ".join(lines)
    address = extract_address(lines)
    if re.search(r",\s*(?:AZ|CA|CO|FL|IL|NY|OK|TN|UT|VA|WA)\b", address, re.I):
        return None

    date_text = " ".join([listing["title"], listing["listing_text"], title])
    # Detail pages often put the event date in the title/subtitle, before the description.
    if title_node:
        subtitle = title_node.find_next(["h2", "h3", "p"])
        if subtitle:
            date_text += " " + clean(subtitle.get_text(" ", strip=True))
    dates = parse_dates(date_text)
    if dates is None:
        # Check the descriptive text for common full-date formats.
        dates = parse_dates(" ".join(lines[:30]))
    if dates is None:
        raise ValueError(f"Could not parse event dates for {title!r}")

    start, end = dates
    if end < date.today():
        return None
    # The browse page is Texas-filtered. Require explicit Texas address evidence
    # when the listing detail provides a location block.
    if address and not re.search(r"\bTX\b|Texas", address, re.I):
        # CoinZip sometimes omits the state beside the address; listing location
        # metadata may still state the event's Texas city.
        if not re.search(r",\s*TX\b|\bTexas\b", state_evidence, re.I):
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


def key(row):
    return tuple(clean(row[c]).casefold() for c in ("Start", "Event", "Venue/Address"))


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
        old = {key(row): row for row in previous}
        new = {key(row): row for row in events}
        changes = []
        if has_snapshot:
            for k in new.keys() - old.keys():
                row = new[k].copy()
                row["Change"] = "NEW"
                changes.append(row)
            for k in old.keys() - new.keys():
                row = old[k].copy()
                row["Change"] = "REMOVED"
                changes.append(row)
        changes.sort(key=lambda row: (row["Start"], row["Event"].casefold(), row["Change"]))
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

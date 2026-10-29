import json
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

TCDB_URL = (
    "https://www.tcdb.com/CardShows.cfm"
    "?MODE=Location&State=TX&Display=Texas"
    "&Country=United%20States"
)

ONTREASURE_URL = "https://www.ontreasure.com/events?states=TX"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}

OUTPUT = Path("output")
OUTPUT.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = Path("data/previous_snapshot.csv")

COLUMNS = [
    "Start Date",
    "End Date",
    "Event Name",
    "Event Location",
    "Source Website",
]
CHANGE_COLUMNS = ["Change Type"] + COLUMNS

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# --------------------------------------------------
# COMMON HELPERS
# --------------------------------------------------

def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def get_soup(url):
    response = SESSION.get(url, timeout=40)
    response.raise_for_status()
    if not response.text.strip():
        raise RuntimeError(f"Empty response from {url}")
    return BeautifulSoup(response.text, "html.parser")


def event_counter(df):
    return Counter(
        map(
            tuple,
            df[COLUMNS].fillna("").astype(str).to_numpy().tolist(),
        )
    )


def load_previous_snapshot():
    if not SNAPSHOT_PATH.exists() or SNAPSHOT_PATH.stat().st_size == 0:
        return None

    old = pd.read_csv(
        SNAPSHOT_PATH, dtype=str, keep_default_na=False
    )

    # Migrate the older TCDB-only snapshot without making every
    # existing TCDB row look new merely because a source column was added.
    if "Source Website" not in old.columns:
        old["Source Website"] = "TCDB"

    for col in COLUMNS:
        if col not in old.columns:
            old[col] = ""

    return old[COLUMNS].fillna("").astype(str)


def compare_snapshots(current, previous):
    if previous is None:
        return pd.DataFrame(columns=CHANGE_COLUMNS)

    before = event_counter(previous)
    after = event_counter(current)
    changes = []

    for row, count in (after - before).items():
        for _ in range(count):
            changes.append({"Change Type": "New", **dict(zip(COLUMNS, row))})

    for row, count in (before - after).items():
        for _ in range(count):
            changes.append({
                "Change Type": "Removed",
                **dict(zip(COLUMNS, row)),
            })

    result = pd.DataFrame(changes, columns=CHANGE_COLUMNS)
    if not result.empty:
        result = result.sort_values(
            ["Change Type", "Start Date", "Event Name"],
            kind="stable",
        ).reset_index(drop=True)
    return result


# --------------------------------------------------
# TCDB PARSER
# --------------------------------------------------

def parse_tcdb_date(text):
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

    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(match.group(1), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def is_tcdb_event_link(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return (
        parsed.netloc.lower().endswith("tcdb.com")
        and parsed.path.lower().endswith("cardshows.cfm")
        and bool(params.get("ID"))
        and params.get("MODE", [""])[0].upper() != "LOCATION"
    )


def scrape_tcdb():
    print("Scraping TCDB...")
    soup = get_soup(TCDB_URL)
    lines = [
        clean(line)
        for line in soup.get_text("\n", strip=True).splitlines()
        if clean(line)
    ]

    links = defaultdict(deque)
    for a in soup.find_all("a", href=True):
        name = clean(a.get_text(" ", strip=True))
        url = urljoin(TCDB_URL, a["href"])
        if name and is_tcdb_event_link(url):
            links[normalize(name)].append(url)

    total_links = sum(len(q) for q in links.values())
    if total_links == 0:
        raise RuntimeError("TCDB validation failed: no event links found.")

    event_names = set(links)
    current_date = ""
    events = []

    for i, line in enumerate(lines):
        date = parse_tcdb_date(line)
        if date:
            current_date = date
            continue

        key = normalize(line)
        if key not in links or not links[key]:
            continue

        links[key].popleft()
        location_parts = []

        for following in lines[i + 1:i + 5]:
            if parse_tcdb_date(following):
                break
            if normalize(following) in event_names:
                break
            location_parts.append(following)
            if re.search(r",\s*[A-Z]{2}\b", following):
                break

        events.append({
            "Start Date": current_date,
            "End Date": current_date,
            "Event Name": line,
            "Event Location": ", ".join(location_parts),
            "Source Website": "TCDB",
        })

    if not events:
        raise RuntimeError(
            "TCDB validation failed: links were found but no event rows matched."
        )

    df = pd.DataFrame(events, columns=COLUMNS).drop_duplicates()
    print(f"TCDB: collected {len(df)} event rows.")
    return df


# --------------------------------------------------
# ONTREASURE PARSER
# --------------------------------------------------

MONTHS = (
    "January February March April May June July August "
    "September October November December"
).split()
MONTH_PATTERN = "|".join(
    [m for month in MONTHS for m in (month, month[:3])]
)


def parse_month_day_year(text, default_year):
    pattern = (
        rf"\b({MONTH_PATTERN})\.?\s+(\d{{1,2}})"
        rf"(?:,?\s+(\d{{4}}))?\b"
    )
    dates = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        month_text, day_text, year_text = match.groups()
        try:
            month_number = next(
                i for i, month in enumerate(MONTHS, 1)
                if month.lower().startswith(month_text.lower()[:3])
            )
            year = int(year_text) if year_text else default_year
            dates.append(datetime(year, month_number, int(day_text)))
        except (ValueError, StopIteration):
            continue
    return dates


def event_detail_links(soup):
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        url = urljoin(ONTREASURE_URL, a["href"])
        parsed = urlparse(url)

        if parsed.netloc.lower() != "www.ontreasure.com":
            continue
        if not parsed.path.startswith("/events/"):
            continue
        if parsed.path.rstrip("/").endswith("/tickets"):
            continue
        if url in seen:
            continue

        seen.add(url)
        found.append(url)

    return found


def find_event_jsonld(soup):
    """Use structured event data when the site provides it."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if "@graph" in item and isinstance(item["@graph"], list):
                candidates.extend(item["@graph"])
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                is_event = "Event" in item_type
            else:
                is_event = item_type == "Event"
            if is_event:
                return item
    return None


def scrape_ontreasure_detail(url):
    soup = get_soup(url)
    json_event = find_event_jsonld(soup)

    page_text = clean(soup.get_text(" ", strip=True))
    title_tag = soup.find("h1")
    title = clean(title_tag.get_text(" ", strip=True)) if title_tag else ""

    if not title:
        og_title = soup.find("meta", property="og:title")
        title = clean(og_title.get("content", "")) if og_title else ""
    if not title and json_event:
        title = clean(json_event.get("name", ""))

    if not title:
        raise RuntimeError(f"OnTreasure event page has no title: {url}")

    start_date = ""
    end_date = ""
    location = ""

    if json_event:
        start_value = clean(json_event.get("startDate", ""))
        end_value = clean(json_event.get("endDate", ""))

        try:
            start_date = datetime.fromisoformat(
                start_value.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
        try:
            end_date = datetime.fromisoformat(
                end_value.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

        loc_data = json_event.get("location", {})
        if isinstance(loc_data, dict):
            address = loc_data.get("address", {})
            if isinstance(address, dict):
                location = ", ".join(
                    clean(address.get(k, ""))
                    for k in ("streetAddress", "addressLocality",
                              "addressRegion", "postalCode")
                    if clean(address.get(k, ""))
                )
            elif isinstance(address, str):
                location = clean(address)
            if not location:
                location = clean(loc_data.get("name", ""))

    # Fallback: extract dates from the event heading/page text.
    if not start_date:
        current_year = datetime.now().year
        year_match = re.search(r"\b20\d{2}\b", title)
        if year_match:
            current_year = int(year_match.group(0))

        dates = parse_month_day_year(title, current_year)
        if not dates:
            dates = parse_month_day_year(page_text[:2500], current_year)

        if dates:
            start_date = min(dates).strftime("%Y-%m-%d")
            end_date = max(dates).strftime("%Y-%m-%d")

    if not end_date:
        end_date = start_date

    # Fallback: use the page's location metadata or Texas city/state text.
    if not location:
        for meta_name in ("og:description", "description"):
            meta = soup.find("meta", property=meta_name) or soup.find(
                "meta", attrs={"name": meta_name}
            )
            if meta and meta.get("content"):
                description = clean(meta["content"])
                match = re.search(
                    r"([A-Za-z .'-]+,\s*TX(?:\s+\d{5})?)",
                    description,
                )
                if match:
                    location = match.group(1)
                    break

    if not location:
        match = re.search(
            r"\b([A-Za-z .'-]+,\s*TX(?:\s+\d{5})?)\b",
            page_text[:5000],
        )
        if match:
            location = match.group(1)

    return {
        "Start Date": start_date,
        "End Date": end_date,
        "Event Name": title,
        "Event Location": location,
        "Source Website": "OnTreasure",
    }


def scrape_ontreasure():
    print("Scraping OnTreasure...")
    soup = get_soup(ONTREASURE_URL)
    links = event_detail_links(soup)

    if not links:
        raise RuntimeError(
            "OnTreasure validation failed: no event detail links found. "
            "The site may require JavaScript or may have changed its layout."
        )

    events = []
    errors = []

    for url in links:
        try:
            event = scrape_ontreasure_detail(url)
            # Only keep listings with at least a usable event date.
            if not event["Start Date"]:
                errors.append(f"No date parsed: {url}")
                continue
            events.append(event)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    # Do not treat a partially scraped source as complete. This prevents
    # a partial page or temporary failure from making events look removed.
    if errors:
        preview = "\n".join(errors[:10])
        raise RuntimeError(
            f"OnTreasure validation failed: {len(errors)} of {len(links)} "
            f"event pages could not be parsed. Snapshot will not be updated.\n"
            f"{preview}"
        )

    if not events:
        raise RuntimeError("OnTreasure validation failed: no event rows parsed.")

    df = pd.DataFrame(events, columns=COLUMNS).drop_duplicates()
    print(f"OnTreasure: collected {len(df)} event rows.")
    return df


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    # Both sources must pass validation before the combined snapshot changes.
    tcdb_df = scrape_tcdb()
    ontreasure_df = scrape_ontreasure()

    current = pd.concat([tcdb_df, ontreasure_df], ignore_index=True)
    current = current[COLUMNS].fillna("").astype(str).drop_duplicates()

    current = current.sort_values(
        ["Start Date", "Event Name", "Source Website"],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = load_previous_snapshot()
    changes = compare_snapshots(current, previous)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    current.to_excel(OUTPUT / "texas_card_shows.xlsx", index=False)
    current.to_csv(OUTPUT / "texas_card_shows.csv", index=False)
    changes.to_excel(OUTPUT / "daily_changes.xlsx", index=False)
    changes.to_csv(OUTPUT / "daily_changes.csv", index=False)

    with open(OUTPUT / "scrape_audit.txt", "w", encoding="utf-8") as f:
        f.write(f"Run time: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"TCDB URL: {TCDB_URL}\n")
        f.write(f"OnTreasure URL: {ONTREASURE_URL}\n")
        f.write(f"TCDB rows: {len(tcdb_df)}\n")
        f.write(f"OnTreasure rows: {len(ontreasure_df)}\n")
        f.write(f"Combined rows: {len(current)}\n")
        f.write(f"New rows: {int((changes['Change Type'] == 'New').sum()) if not changes.empty else 0}\n")
        f.write(f"Removed rows: {int((changes['Change Type'] == 'Removed').sum()) if not changes.empty else 0}\n")
        f.write("\nNote: matching is based on all exported columns, including source.\n")

    # Update the persistent baseline only after both sources and exports succeed.
    current.to_csv(SNAPSHOT_PATH, index=False)

    print(f"Combined rows: {len(current)}")
    print(f"New rows: {int((changes['Change Type'] == 'New').sum()) if not changes.empty else 0}")
    print(f"Removed rows: {int((changes['Change Type'] == 'Removed').sum()) if not changes.empty else 0}")
    print("Reports saved in output/.")


if __name__ == "__main__":
    main()

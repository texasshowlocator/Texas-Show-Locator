
import re
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
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})",
    re.IGNORECASE
)


def get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def get_event_urls(soup):
    urls = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]

        if "CardShows.cfm" in href and "ID=" in href.upper():
            full_url = urljoin(SOURCE_URL, href)

            if "MODE=VIEW" in full_url.upper():
                urls.add(full_url)

    return sorted(urls)


def scrape_event(url):
    soup = get_soup(url)
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Find the first heading that appears to be the event name.
    heading = soup.find(["h1", "h2", "h3"])
    event_name = (
        heading.get_text(" ", strip=True)
        if heading else soup.title.get_text(" ", strip=True)
    )

    date_match = DATE_RE.search(text)
    event_date = date_match.group(2) if date_match else ""

    city = ""
    venue = ""

    # Find a line identifying a Texas city.
    for i, line in enumerate(lines):
        if re.search(r",\s*TX(?:\s|$)", line, re.IGNORECASE):
            city = re.sub(
                r",\s*TX.*$", "", line, flags=re.IGNORECASE
            ).strip()

            # The venue is often listed on a line above the address.
            if i >= 1:
                venue = lines[i - 1]
            break

    return {
        "Start Date": event_date,
        "End Date": "",
        "Event Name": event_name,
        "Venue": venue,
        "City": city,
        "State": "TX",
        "Source Website": "TCDB",
        "Event URL": url,
        "Review Needed": (
            "Check date, venue, and city"
            if not event_date or not city or not venue else ""
        ),
    }


def main():
    soup = get_soup(SOURCE_URL)
    event_urls = get_event_urls(soup)

    if not event_urls:
        raise RuntimeError(
            "No event detail links found. "
            "Check whether TCDB changed its page layout."
        )

    events = []

    for url in event_urls:
        try:
            events.append(scrape_event(url))
        except requests.RequestException as exc:
            print(f"Could not retrieve {url}: {exc}")

    if not events:
        raise RuntimeError("No event details could be retrieved.")

    df = pd.DataFrame(events)
    df = df.drop_duplicates(subset=["Event URL"])
    df.to_csv(OUTPUT / "texas_card_shows.csv", index=False)

    with pd.ExcelWriter(
        OUTPUT / "texas_card_shows.xlsx",
        engine="openpyxl"
    ) as writer:
        df.to_excel(writer, index=False, sheet_name="Texas Shows")
        ws = writer.sheets["Texas Shows"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        for cells in ws.columns:
            width = max(len(str(c.value or "")) for c in cells)
            ws.column_dimensions[cells[0].column_letter].width = min(
                width + 2, 50
            )

    print(f"Event pages found: {len(event_urls)}")
    print(f"Events extracted: {len(df)}")
    print("Files saved in output/")


if __name__ == "__main__":
    main()

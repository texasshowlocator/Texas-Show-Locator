def scrape_event(url):
    soup = get_soup(url)
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    heading = soup.find(["h1", "h2", "h3"])
    event_name = (
        heading.get_text(" ", strip=True)
        if heading else "Unknown Event"
    )

    # Find the primary date listed for this event.
    date_match = DATE_RE.search(text)
    start_date = ""
    end_date = ""

    if date_match:
        start_date = datetime.strptime(
            date_match.group(2), "%B %d, %Y"
        ).date()
        end_date = start_date

        # Look for a date range such as July 4-5, 2026.
        range_pattern = re.compile(
            r"\b(January|February|March|April|May|June|"
            r"July|August|September|October|November|December)"
            r"\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s+(\d{4})\b",
            re.IGNORECASE,
        )

        for match in range_pattern.finditer(text):
            month, first_day, last_day, year = match.groups()

            try:
                range_start = datetime.strptime(
                    f"{month} {first_day} {year}", "%B %d %Y"
                ).date()

                range_end = datetime.strptime(
                    f"{month} {last_day} {year}", "%B %d %Y"
                ).date()

                # Only accept a range that begins on the event's
                # primary date, avoiding unrelated dates in notes.
                if range_start == start_date and range_end >= range_start:
                    end_date = range_end
                    break
            except ValueError:
                continue

    city = ""
    venue = ""

    for i, line in enumerate(lines):
        if re.search(r",\s*TX(?:\s|$)", line, re.IGNORECASE):
            city = re.sub(
                r",\s*TX.*$", "", line, flags=re.IGNORECASE
            ).strip()

            if i >= 1:
                venue = lines[i - 1]
            break

    review = []

    if not start_date:
        review.append("Start date not found")
    if not venue:
        review.append("Venue not detected")
    if not city:
        review.append("City not detected")

    return {
        "Start Date": start_date,
        "End Date": end_date,
        "Event Name": event_name,
        "Venue": venue,
        "City": city,
        "State": "TX",
        "Source Website": "TCDB",
        "Event URL": url,
        "Review Needed": "; ".join(review),
    }

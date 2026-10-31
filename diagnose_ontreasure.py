import json
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

URL = "https://www.ontreasure.com/events?states=TX"
OUT = Path("output/ontreasure_diagnostics")
OUT.mkdir(parents=True, exist_ok=True)

def main():
    network = []
    json_responses = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )

        def record_response(response):
            try:
                url = response.url
                if any(token in url.lower() for token in (
                    "api", "event", "graphql", "trpc", "search"
                )):
                    network.append({
                        "url": url,
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    })
                content_type = response.headers.get("content-type", "")
                if "json" in content_type.lower():
                    body = response.text()
                    if len(body) <= 2_000_000:
                        try:
                            payload = json.loads(body)
                            json_responses.append({
                                "url": url,
                                "status": response.status,
                                "body": payload,
                            })
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", record_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        # Scroll through lazy-loaded results until the page stops growing.
        previous_count = -1
        stable_rounds = 0
        for _ in range(40):
            count = page.locator('a[href*="/events/"]').count()
            if count == previous_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 4:
                break
            previous_count = count
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)

        links = page.locator('a[href*="/events/"]').evaluate_all(
            """els => els.map(a => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').trim()
            }))"""
        )

        scripts = page.locator("script").evaluate_all(
            """els => els.map(s => ({
                type: s.type || '',
                id: s.id || '',
                text: (s.textContent || '').slice(0, 500000)
            })).filter(x => x.text)"""
        )

        result = {
            "page_url": page.url,
            "page_title": page.title(),
            "page_text": page.locator("body").inner_text(),
            "event_links": links,
            "network_requests": network,
            "json_responses": json_responses,
            "inline_scripts": scripts,
        }

        (OUT / "diagnostics.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (OUT / "rendered_page.html").write_text(
            page.content(),
            encoding="utf-8",
        )
        page.screenshot(path=str(OUT / "page.png"), full_page=True)
        print(f"Captured {len(links)} event links and {len(json_responses)} JSON responses.")
        browser.close()

if __name__ == "__main__":
    main()

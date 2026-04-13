import requests
from bs4 import BeautifulSoup
import json
import time

BASE_URLS = [
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ALPS-Scraper/1.0)"
}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            res = requests.get(url, headers=HEADERS, timeout=20)
            res.raise_for_status()
            return res.text
        except Exception as e:
            print(f"Retry {i+1}/{retries} failed for {url}: {e}")
            time.sleep(2)
    return None

def extract_tables(html, url):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    tables = soup.find_all("table")

    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]

        for row in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]

            if not cols:
                continue

            event = {
                "source_url": url
            }

            for i in range(min(len(headers), len(cols))):
                event[headers[i]] = cols[i]

            results.append(event)

    return results

def scrape_all():
    all_events = []

    for url in BASE_URLS:
        print(f"Scraping {url}")
        html = fetch(url)

        if not html:
            continue

        events = extract_tables(html, url)
        all_events.extend(events)

    return all_events

if __name__ == "__main__":
    data = scrape_all()

    with open("alps_sourcing_events.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Done. Extracted {len(data)} events.")

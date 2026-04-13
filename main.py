import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ---------------- #

URLS = [
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9"
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ---------------- GOOGLE AUTH ---------------- #

def get_google_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if not creds_json:
        raise Exception("Missing GOOGLE_CREDENTIALS_JSON environment variable")

    creds_dict = json.loads(creds_json)

    credentials = Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )

    return credentials

# ---------------- SCRAPER ---------------- #

def fetch(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        return res.text
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return ""


def extract_events(html, url):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # -------------------------------
    # 1. Try table-based extraction
    # -------------------------------
    tables = soup.find_all("table")

    for table in tables:
        headers = [h.get_text(strip=True) for h in table.find_all("th")]

        for tr in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]

            if not cols:
                continue

            row = {"source_url": url}

            if headers and len(headers) == len(cols):
                for i in range(len(headers)):
                    row[headers[i]] = cols[i]
            else:
                # fallback generic columns
                for i, col in enumerate(cols):
                    row[f"column_{i+1}"] = col

            results.append(row)

    # -------------------------------
    # 2. Fallback: extract list/card text
    # -------------------------------
    if not results:
        items = soup.find_all("li")

        for item in items:
            text = item.get_text(" ", strip=True)

            if len(text) > 20:  # avoid junk
                results.append({
                    "source_url": url,
                    "content": text
                })

    return results

# ---------------- GOOGLE SHEETS ---------------- #

def connect_sheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)

    sheet = client.open_by_key("1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg").sheet1
    return sheet


def write_to_sheet(sheet, data):
    sheet.clear()

    if not data:
        print("No data found")
        return

    headers = list(data[0].keys())

    sheet.append_row(headers)

    for row in data:
        sheet.append_row([row.get(h, "") for h in headers])

# ---------------- MAIN ---------------- #

def main():
    all_data = []

    for url in URLS:
        print(f"Scraping: {url}")
        html = fetch(url)

        if html:
            all_data.extend(extract_events(html, url))

    print(f"Total rows found: {len(all_data)}")

    sheet = connect_sheet()
    write_to_sheet(sheet, all_data)

    print("✅ Google Sheet updated successfully")


if __name__ == "__main__":
    main()

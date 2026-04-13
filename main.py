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

    tables = soup.find_all("table")

    for table in tables:
        headers = [h.get_text(strip=True) for h in table.find_all("th")]

        for tr in table.find_all("tr")[1:]:
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]

            if not cols:
                continue

            row = {"source_url": url}

            for i in range(min(len(headers), len(cols))):
                row[headers[i]] = cols[i]

            results.append(row)

    return results

# ---------------- GOOGLE SHEETS ---------------- #

def connect_sheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)

    sheet = client.open("ALPS Sourcing Events").sheet1
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

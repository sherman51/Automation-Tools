import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ---------------- #

URL_SHEET_MAP = {
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/": "National Sourcing Events",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/": "Pharmaceutical Sourcing Events",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9"
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"

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

    current_month = None

    # Walk through page in order
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "table"]):

        # ✅ Capture month headers
        if tag.name in ["h1", "h2", "h3", "h4"]:
            text = tag.get_text(strip=True)

            # Basic check for month/year text
            if any(month in text.lower() for month in [
                "january","february","march","april","may","june",
                "july","august","september","october","november","december"
            ]):
                current_month = text

        # ✅ Process table under that month
        elif tag.name == "table":
            table = tag
            rows = table.find_all("tr")

            if not rows:
                continue

            headers = [h.get_text(strip=True) for h in table.find_all("th")]

            # Fallback header logic
            if not headers:
                first_row_cols = rows[0].find_all(["td", "th"])
                headers = [c.get_text(strip=True) for c in first_row_cols]
                rows = rows[1:]

            for tr in rows:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]

                if not cols:
                    continue

                if cols == headers:
                    continue

                row = {
                    "source_url": url,
                    "month": current_month  # ✅ ADD THIS
                }

                for i in range(min(len(headers), len(cols))):
                    row[headers[i]] = cols[i]

                results.append(row)

    # Fallback (unchanged)
    if not results:
        items = soup.find_all("li")

        for item in items:
            text = item.get_text(" ", strip=True)

            if len(text) > 20:
                results.append({
                    "source_url": url,
                    "content": text
                })

    return results

# ---------------- GOOGLE SHEETS ---------------- #

def connect_spreadsheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=sheet_name,
            rows="1000",
            cols="20"
        )
    return worksheet


def write_to_sheet(sheet, data):
    sheet.clear()

    if not data:
        print("No data found")
        return

    headers = list(data[0].keys())
    rows = [headers] + [
        [row.get(h, "") for h in headers]
        for row in data
    ]

    sheet.update(rows)

# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_spreadsheet()

    for url, sheet_name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")

        html = fetch(url)
        data = []

        if html:
            data = extract_events(html, url)

        print(f"{sheet_name}: {len(data)} rows")

        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
        write_to_sheet(worksheet, data)

    print("✅ Google Sheets updated successfully")


if __name__ == "__main__":
    main()

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

URLS = [
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/",
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------------- SCRAPER ---------------- #

def fetch(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.text


def extract_data(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = []

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

            rows.append(row)

    return rows


# ---------------- GOOGLE SHEETS ---------------- #

def connect_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=scopes
    )

    client = gspread.authorize(creds)

    # MUST match your Google Sheet name exactly
    sheet = client.open("Sourcing Events").sheet1

    return sheet


def write_sheet(sheet, data):
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
    all_rows = []

    for url in URLS:
        print(f"Scraping: {url}")
        html = fetch(url)
        all_rows.extend(extract_data(html, url))

    print(f"Total rows found: {len(all_rows)}")

    sheet = connect_sheet()
    write_sheet(sheet, all_rows)

    print("✅ Google Sheet updated successfully")


if __name__ == "__main__":
    main()

import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import time
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

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

ARIBA_URL = "https://service.ariba.com/Sourcing.aw/advancesearch"
SESSION_FILE = "ariba_state.json"

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


def connect_spreadsheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# ---------------- SHEETS ---------------- #

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

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "table"]):

        if tag.name in ["h1", "h2", "h3", "h4"]:
            text = tag.get_text(strip=True)

            if any(month in text.lower() for month in [
                "january","february","march","april","may","june",
                "july","august","september","october","november","december"
            ]):
                current_month = text

        elif tag.name == "table":
            table = tag
            rows = table.find_all("tr")

            if not rows:
                continue

            headers = [h.get_text(strip=True) for h in table.find_all("th")]

            if not headers:
                first_row_cols = rows[0].find_all(["td", "th"])
                headers = [c.get_text(strip=True) for c in first_row_cols]
                rows = rows[1:]

            for tr in rows:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]

                if not cols:
                    continue

                row = {"source_url": url}

                if current_month:
                    row["PERIOD"] = current_month

                for i in range(min(len(headers), len(cols))):
                    row[headers[i]] = cols[i]

                results.append(row)

    return results


# ---------------- RFP EXTRACTION ---------------- #

def get_rfp_numbers(spreadsheet):
    sheet = spreadsheet.worksheet("Pharmaceutical Sourcing Events")
    data = sheet.get_all_records()

    rfps = []

    for row in data:
        rfp_no = row.get("RFP No.")
        if rfp_no:
            rfps.append(str(rfp_no).strip())

    return list(set(rfps))


# ---------------- ARIBA LOGIN (ONE TIME) ---------------- #

def init_ariba_session():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://service.ariba.com/Sourcing.aw/")

        print("👉 Please login manually (SSO/MFA)...")
        page.wait_for_timeout(180000)  # 3 minutes

        context.storage_state(path=SESSION_FILE)

        print("✅ Ariba session saved")
        browser.close()


# ---------------- ARIBA SEARCH ---------------- #

def search_ariba(keyword):
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        page.goto(ARIBA_URL)
        page.wait_for_timeout(3000)

        page.fill("input[type='search']", keyword)
        page.keyboard.press("Enter")

        page.wait_for_timeout(5000)

        items = page.query_selector_all(".search-result-item")

        for item in items:
            try:
                title_el = item.query_selector(".title")
                link_el = item.query_selector("a")

                title = title_el.inner_text() if title_el else ""
                link = link_el.get_attribute("href") if link_el else ""

                results.append({
                    "RFP No": keyword,
                    "title": title,
                    "link": link
                })

            except:
                continue

        browser.close()

    return results


def build_tender_alerts(rfp_list):
    all_results = []

    for rfp in rfp_list:
        print(f"Searching Ariba: {rfp}")

        try:
            results = search_ariba(rfp)
            all_results.extend(results)

        except Exception as e:
            print(f"Error for {rfp}: {e}")

        time.sleep(1.5)

    return all_results


# ---------------- TENDER ALERTS ---------------- #

def write_tender_alerts(spreadsheet, data):
    sheet = get_or_create_worksheet(spreadsheet, "Tender Alerts")
    write_to_sheet(sheet, data)


# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_spreadsheet()

    # STEP 1: SCRAPE ALPS
    all_scraped_data = []

    for url, sheet_name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")

        html = fetch(url)
        data = extract_events(html, url) if html else []

        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
        write_to_sheet(worksheet, data)

        all_scraped_data.extend(data)

    # STEP 2: GET RFP NUMBERS
    rfp_list = get_rfp_numbers(spreadsheet)
    print(f"Found {len(rfp_list)} RFP numbers")

    rfp_list = rfp_list[:30]  # safety limit

    # STEP 3: SEARCH ARIBA
    tender_alerts = build_tender_alerts(rfp_list)
    print(f"Found {len(tender_alerts)} Ariba results")

    # STEP 4: WRITE RESULTS
    write_tender_alerts(spreadsheet, tender_alerts)

    print("✅ Pipeline complete")


if __name__ == "__main__":
    main()

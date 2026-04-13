import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import time
import subprocess
import sys
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

    return Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )


def connect_spreadsheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# ---------------- SHEETS ---------------- #

def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")


def write_to_sheet(sheet, data):
    sheet.clear()

    if not data:
        print("⚠️ No data to write")
        return

    headers = list(data[0].keys())
    rows = [headers] + [[row.get(h, "") for h in headers] for row in data]

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

            if any(m in text.lower() for m in [
                "january","february","march","april","may","june",
                "july","august","september","october","november","december"
            ]):
                current_month = text

        elif tag.name == "table":
            rows = tag.find_all("tr")

            if not rows:
                continue

            headers = [h.get_text(strip=True) for h in tag.find_all("th")]

            if not headers:
                first_row = rows[0].find_all(["td", "th"])
                headers = [c.get_text(strip=True) for c in first_row]
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
        rfp = row.get("RFP No.")
        if rfp:
            rfps.append(str(rfp).strip())

    return list(set(rfps))


# ---------------- ARIBA LOGIN ---------------- #

def init_ariba_session():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://service.ariba.com/Sourcing.aw/")

        print("👉 Login manually (SSO/MFA)...")
        page.wait_for_timeout(180000)

        context.storage_state(path=SESSION_FILE)

        print("✅ Session saved")
        browser.close()


def ensure_session_exists():
    """
    Prevents crash when ariba_state.json is missing
    """
    if not os.path.exists(SESSION_FILE):
        print("⚠️ No Ariba session found. Starting login flow...")
        init_ariba_session()


# ---------------- ARIBA SEARCH ---------------- #

def search_ariba(keyword):
    results = []

    ensure_session_exists()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        page.goto(ARIBA_URL)
        page.wait_for_timeout(3000)

        page.fill("input[type='search']", keyword)
        page.keyboard.press("Enter")

        page.wait_for_timeout(5000)

        items = page.query_selector_all(".search-result-item")

        print(f"DEBUG: {keyword} -> {len(items)} results")

        for item in items:
            try:
                title_el = item.query_selector(".title")
                link_el = item.query_selector("a")

                results.append({
                    "RFP No": keyword,
                    "title": title_el.inner_text() if title_el else "",
                    "link": link_el.get_attribute("href") if link_el else ""
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
            print(f"Found {len(results)} results")
            all_results.extend(results)

        except Exception as e:
            print(f"Error for {rfp}: {e}")

        time.sleep(1.5)

    return all_results


# ---------------- TENDER SHEET ---------------- #

def write_tender_alerts(spreadsheet, data):
    sheet = get_or_create_worksheet(spreadsheet, "Tender Alerts")
    sheet.clear()

    if not data:
        print("⚠️ No Tender Alerts found")
        return

    headers = list(data[0].keys())
    rows = [headers] + [[row.get(h, "") for h in headers] for row in data]

    sheet.update(rows)


# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_spreadsheet()

    # STEP 1: SCRAPE ALPS
    for url, sheet_name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")

        html = fetch(url)
        data = extract_events(html, url) if html else []

        print(f"{sheet_name}: {len(data)} rows")

        sheet = get_or_create_worksheet(spreadsheet, sheet_name)
        write_to_sheet(sheet, data)

    # STEP 2: GET RFPs
    rfp_list = get_rfp_numbers(spreadsheet)
    print(f"Found RFPs: {len(rfp_list)}")

    rfp_list = rfp_list[:30]

    print("RFP SAMPLE:", rfp_list[:5])

    # STEP 3: ARIBA SEARCH
    tender_alerts = build_tender_alerts(rfp_list)

    print(f"Total Tender Alerts: {len(tender_alerts)}")

    # STEP 4: WRITE RESULTS
    write_tender_alerts(spreadsheet, tender_alerts)

    print("✅ DONE")


if __name__ == "__main__":
    main()

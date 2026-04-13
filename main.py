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
    "User-Agent": "Mozilla/5.0"
}

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

ARIBA_URL = "https://service.ariba.com/Sourcing.aw/"
SESSION_FILE = "ariba_state.json"


# ---------------- GOOGLE SHEETS ---------------- #

def get_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise Exception("Missing GOOGLE_CREDENTIALS_JSON")

    creds_dict = json.loads(creds_json)

    return Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )


def connect_sheet():
    client = gspread.authorize(get_creds())
    return client.open_by_key(SPREADSHEET_ID)


def get_sheet(spreadsheet, name):
    try:
        return spreadsheet.worksheet(name)
    except:
        return spreadsheet.add_worksheet(title=name, rows="1000", cols="20")


# ---------------- SCRAPER ---------------- #

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


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
                headers = [c.get_text(strip=True) for c in rows[0].find_all("td")]
                rows = rows[1:]

            for r in rows:
                cols = [c.get_text(strip=True) for c in r.find_all("td")]
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

def get_rfps(sheet):
    data = sheet.get_all_records()
    rfps = []

    for r in data:
        val = r.get("RFP No.")
        if val:
            rfps.append(str(val).strip())

    return list(set(rfps))


# ---------------- ARIBA SESSION ---------------- #

def ensure_session():
    if not os.path.exists(SESSION_FILE):
        print("⚠️ No session found. Run login once.")
        return False
    return True


def init_session():
    print("👉 Opening browser for login...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(ARIBA_URL)

        print("🔐 Please login manually...")
        page.wait_for_timeout(180000)

        context.storage_state(path=SESSION_FILE)

        print("✅ Session saved")
        browser.close()


# ---------------- ARIBA SEARCH ---------------- #

def search_ariba(rfp):
    if not ensure_session():
        return []

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        context = browser.new_context(storage_state=SESSION_FILE)
        page = context.new_page()

        page.goto(ARIBA_URL)
        page.wait_for_timeout(4000)

        try:
            page.fill("input[type='search']", rfp)
            page.keyboard.press("Enter")
        except:
            browser.close()
            return []

        page.wait_for_timeout(5000)

        items = page.query_selector_all(".search-result-item")

        for i in items:
            try:
                title = i.query_selector(".title")
                link = i.query_selector("a")

                results.append({
                    "RFP No": rfp,
                    "title": title.inner_text() if title else "",
                    "link": link.get_attribute("href") if link else ""
                })
            except:
                continue

        browser.close()

    return results


def build_alerts(rfps):
    all_data = []

    for r in rfps:
        print(f"Searching Ariba: {r}")
        try:
            res = search_ariba(r)
            print(f"Found {len(res)} results")
            all_data.extend(res)
        except Exception as e:
            print(f"Error {r}: {e}")

        time.sleep(1)

    return all_data


# ---------------- WRITE SHEET ---------------- #

def write_sheet(sheet, data):
    sheet.clear()

    if not data:
        print("⚠️ No Tender Alerts found")
        return

    headers = list(data[0].keys())
    rows = [headers] + [[r.get(h, "") for h in headers] for r in data]

    sheet.update(rows)


# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_sheet()

    # STEP 1 - SCRAPE
    for url, name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")

        html = fetch(url)
        data = extract_events(html, url)

        print(f"{name}: {len(data)} rows")

        sheet = get_sheet(spreadsheet, name)
        write_sheet(sheet, data)

    # STEP 2 - RFPs
    sheet = spreadsheet.worksheet("Pharmaceutical Sourcing Events")
    rfps = get_rfps(sheet)

    print(f"Found RFPs: {len(rfps)}")
    print("Sample:", rfps[:5])

    # STEP 3 - ARIBA
    alerts = build_alerts(rfps[:20])

    print(f"Total Alerts: {len(alerts)}")

    # STEP 4 - WRITE
    write_sheet(get_sheet(spreadsheet, "Tender Alerts"), alerts)

    print("✅ DONE")


if __name__ == "__main__":
    main()

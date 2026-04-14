import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import re
import time
from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------- CONFIG ---------------- #

URL_SHEET_MAP = {
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/": "National Sourcing Events",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/": "Pharmaceutical Sourcing Events",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9"
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"

ARIBA_LOGIN_URL = "https://service.ariba.com/Authenticator.aw/ad/ssoIDP"

ARIBA_USERNAME = os.getenv("ARIBA_USERNAME", "")
ARIBA_PASSWORD = os.getenv("ARIBA_PASSWORD", "")

TENDER_ALERTS_SHEET = "Tender Alerts"

# ---------------- GOOGLE AUTH ---------------- #

def get_google_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise Exception("Missing GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

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

    for tag in soup.find_all(["h1","h2","h3","h4","table"]):
        if tag.name.startswith("h"):
            text = tag.get_text(strip=True)
            if any(m in text.lower() for m in [
                "january","february","march","april","may","june",
                "july","august","september","october","november","december"
            ]):
                current_month = text

        elif tag.name == "table":
            rows = tag.find_all("tr")
            headers = [h.get_text(strip=True) for h in tag.find_all("th")]

            if not headers and rows:
                headers = [c.get_text(strip=True) for c in rows[0].find_all(["td","th"])]
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

def extract_rfp_numbers(data):
    rfp_set = set()
    pattern = re.compile(r'\b(?:RFP|GPOR)[-\s]?\w+\b', re.IGNORECASE)

    for row in data:
        for val in row.values():
            if isinstance(val, str):
                matches = pattern.findall(val)
                for m in matches:
                    rfp_set.add(m.strip())

    return list(rfp_set)

# ---------------- GOOGLE SHEETS ---------------- #

def connect_spreadsheet():
    client = gspread.authorize(get_google_creds())
    return client.open_by_key(SPREADSHEET_ID)

def get_or_create_worksheet(spreadsheet, name):
    try:
        return spreadsheet.worksheet(name)
    except:
        return spreadsheet.add_worksheet(title=name, rows="1000", cols="20")

def write_to_sheet(sheet, data):
    sheet.clear()
    if not data:
        return
    headers = list(data[0].keys())
    rows = [headers] + [[row.get(h,"") for h in headers] for row in data]
    sheet.update(rows)

# ---------------- SELENIUM ---------------- #

def build_driver(headless=True):
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)

    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    return driver

# ---------------- ARIBA LOGIN ---------------- #

ARIBA_COOKIES_JSON = os.getenv("ARIBA_COOKIES_JSON", "")

def load_cookies(driver):
    """Load saved cookies into the browser session."""
    if not ARIBA_COOKIES_JSON:
        raise Exception("Missing ARIBA_COOKIES_JSON environment variable")

    cookies = json.loads(ARIBA_COOKIES_JSON)

    # Must navigate to the domain first before setting cookies
    driver.get("https://service.ariba.com")
    time.sleep(3)

    for cookie in cookies:
        # Selenium only accepts specific cookie fields — strip extras
        cleaned = {
            "name":   cookie["name"],
            "value":  cookie["value"],
            "domain": cookie["domain"],
            "path":   cookie.get("path", "/"),
            "secure": cookie.get("secure", False),
        }

        # Add expiry only if it exists and is a non-session cookie
        if "expirationDate" in cookie:
            cleaned["expiry"] = int(cookie["expirationDate"])

        try:
            driver.add_cookie(cleaned)
        except Exception as e:
            print(f"⚠️ Skipped cookie {cookie['name']}: {e}")

    print(f"✓ Loaded {len(cookies)} cookies")


def ariba_login(driver, wait):
    print("→ Loading Ariba session via cookies...")

    load_cookies(driver)

    # Navigate to the main supplier page to activate the session
    driver.get("https://service.ariba.com/Supplier.aw/ad/landing")
    time.sleep(5)

    driver.save_screenshot("/tmp/ariba_step1_after_cookies.png")
    print("URL:", driver.current_url)
    print("Title:", driver.title)

    # Check if we're logged in (not redirected back to login page)
    if "Authenticator" in driver.current_url or "ssoIDP" in driver.current_url:
        raise Exception(
            "Cookie session expired or invalid — redirected to login. "
            "Please refresh your ariba_cookies.json."
        )

    print("✓ Session restored via cookies")

# ---------------- ARIBA SEARCH ---------------- #

def ariba_search_rfp(driver, wait, rfp_no):
    print(f"Searching {rfp_no}")

    # Navigate to dashboard
    driver.get("https://portal.us.bn.cloud.ariba.com/dashboard/")
    time.sleep(3)

    # Step 1: Type RFP number into the "By Product" search box
    search_input = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "input.search-input[placeholder='By Product']"))
    )
    search_input.clear()
    search_input.click()
    time.sleep(1)
    search_input.send_keys(rfp_no)
    time.sleep(1)
    search_input.send_keys(Keys.RETURN)
    time.sleep(4)  # Wait for results page to load

    # Step 2: Filter by Singapore in the location multi-input
    try:
        location_input = wait.until(
            EC.presence_of_element_located((By.ID, "__xmlview1--idLocationFilterMultiInput-inner"))
        )
        location_input.click()
        time.sleep(1)
        location_input.send_keys("Singapore")
        time.sleep(2)  # Wait for dropdown suggestions to appear

        # Select the first suggestion from the SAP UI dropdown
        suggestion = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".sapMSuggestionItem, [class*='suggestion'], [class*='popover'] li"))
        )
        suggestion.click()
        time.sleep(3)  # Wait for filtered results

    except Exception as e:
        print(f"  ⚠️ Could not apply Singapore filter: {e}")

    # DEBUG: save page for inspection (remove once working)
    with open(f"/tmp/ariba_{rfp_no.replace(' ', '_')}.html", "w") as f:
        f.write(driver.page_source)

    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Step 3: Extract lead titles from results
    lead_title = ""
    for selector in [
        "[class*='sapMLnk']",         # SAP UI5 link elements
        "[class*='title']",
        "[class*='rfx-name']",
        "[class*='leadTitle']",
        "[class*='itemTitle']",
        ".sapMListTblCell",            # SAP table cells
        "[class*='result'] td",
        "h1", "h2", "h3",
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) > 5 and text.lower() not in ("", "ariba", "sap ariba", "by product", "select or type location"):
                lead_title = text
                break

    print(f"  → Title: {lead_title}")

    return {
        "RFP No.": rfp_no,
        "Lead Title": lead_title,
        "Ariba URL": driver.current_url
    }

# ---------------- MAIN ---------------- #

def run_ariba_search(rfps):
    driver = build_driver(headless=True)
    wait = WebDriverWait(driver, 20)

    results = []

    try:
        ariba_login(driver, wait)

        for r in rfps:
            results.append(ariba_search_rfp(driver, wait, r))

    finally:
        driver.quit()

    return results

def main():
    sheet = connect_spreadsheet()
    pharma_data = []

    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)

        write_to_sheet(get_or_create_worksheet(sheet, name), data)

        if "pharmaceutical" in name.lower():
            pharma_data = data

    rfps = extract_rfp_numbers(pharma_data)
    print("RFPs:", rfps)

    tender_data = run_ariba_search(rfps)

    write_to_sheet(
        get_or_create_worksheet(sheet, TENDER_ALERTS_SHEET),
        tender_data
    )

if __name__ == "__main__":
    main()

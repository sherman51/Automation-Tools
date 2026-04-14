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

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)

    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    return driver

# ---------------- ARIBA LOGIN ---------------- #

def ariba_login(driver, wait):
    print("→ Opening Ariba login...")
    driver.get(ARIBA_LOGIN_URL)

    # wait for page to fully load
    time.sleep(5)

    print("URL:", driver.current_url)
    print("Title:", driver.title)

    driver.save_screenshot("/tmp/ariba_login_page.png")

    # ---------------- STEP 1: USERNAME ---------------- #
    try:
        username = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@name='userid' or @type='email' or contains(@id,'user')]"))
        )
    except:
        raise Exception("Username field not found")

    username.clear()
    username.send_keys(ARIBA_USERNAME)
    print("✓ Username entered")

    # click NEXT
    try:
        next_btn = wait.until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[contains(translate(., 'NEXT', 'next'),'next')] | //input[@value='Next']"
            ))
        )
        next_btn.click()
        print("✓ Clicked NEXT")
    except:
        # fallback JS click (more reliable on enterprise sites)
        print("⚠️ Normal click failed, using JS click fallback")
        driver.execute_script("""
            let buttons = [...document.querySelectorAll('button, input[type=submit]')];
            let next = buttons.find(b => (b.innerText || b.value || '').toLowerCase().includes('next'));
            if (next) next.click();
        """)

    # ---------------- STEP 2: PASSWORD ---------------- #
    try:
        password = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='password']"))
        )
    except:
        driver.save_screenshot("/tmp/ariba_no_password.png")
        with open("/tmp/ariba_after_next.html", "w") as f:
            f.write(driver.page_source)
        raise Exception("Password field never appeared after NEXT")

    password.clear()
    password.send_keys(ARIBA_PASSWORD)
    print("✓ Password entered")

    # submit login
    try:
        login_btn = driver.find_element(By.XPATH, "//button[contains(.,'Login') or contains(.,'Sign') or @type='submit']")
        login_btn.click()
    except:
        password.send_keys(Keys.RETURN)

    print("✓ Login submitted")

    # wait for redirect to authenticated page
    time.sleep(8)

    print("Final URL:", driver.current_url)
    driver.save_screenshot("/tmp/ariba_after_login.png")

    # simple validation
    if "login" in driver.current_url.lower():
        raise Exception("Login failed - still on login page")

    print("✅ Login successful")

# ---------------- ARIBA SEARCH ---------------- #

def ariba_search_rfp(driver, wait, rfp_no):
    print(f"Searching {rfp_no}")
    url = f"https://service.ariba.com/Discovery.aw/ad/rfxList?rfxId={rfp_no}"
    driver.get(url)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")

    return {
        "RFP No.": rfp_no,
        "Lead Title": soup.title.text.strip() if soup.title else "",
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

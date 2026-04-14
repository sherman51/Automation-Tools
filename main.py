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
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

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

ARIBA_LOGIN_URL = "https://service.ariba.com/Authenticator.aw"

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

# ---------------- ARIBA REACHABILITY CHECK ---------------- #

def check_ariba_reachable():
    """Check if Ariba is reachable before launching Selenium.
    GitHub Actions runners are sometimes blocked by Ariba's SSO endpoint."""
    try:
        r = requests.get(
            "https://service.ariba.com",
            headers=HEADERS,
            timeout=10
        )
        print(f"✓ Ariba reachable: HTTP {r.status_code}")
        return True
    except Exception as e:
        print(f"✗ Ariba not reachable: {e}")
        return False

# ---------------- SELENIUM ---------------- #

def build_driver(headless=True):
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    # Required flags for CI / headless Linux environments
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Use webdriver_manager to auto-match ChromeDriver to installed Chrome version
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    driver.execute_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    return driver

# ---------------- ARIBA LOGIN ---------------- #

def ariba_login(driver, wait):
    print("→ Opening Ariba login...")

    driver.delete_all_cookies()
    driver.get("about:blank")
    time.sleep(1)

    driver.get("https://service.ariba.com/Authenticator.aw")
    time.sleep(5)

    print("URL:", driver.current_url)
    print("Title:", driver.title)

    driver.save_screenshot("/tmp/ariba_login_page.png")
    with open("/tmp/ariba_login_page.html", "w") as f:
        f.write(driver.page_source)

    # Username
    username = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter Username' or @name='UserName' or @id='UserName']")
    ))
    username.clear()
    username.send_keys(ARIBA_USERNAME)
    print("✓ Username entered")

    # Password
    password = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter Password' or @type='password']")
    ))
    password.clear()
    password.send_keys(ARIBA_PASSWORD)
    print("✓ Password entered")

    # Click Login button — try all visible buttons
    clicked = False
    buttons = driver.find_elements(By.XPATH, "//button | //input[@type='submit'] | //input[@type='button']")
    for b in buttons:
        if b.is_displayed():
            try:
                driver.execute_script("arguments[0].click();", b)
                print(f"✓ Clicked login button: {b.tag_name} / {b.get_attribute('value')!r} / {b.text!r}")
                clicked = True
                break
            except:
                continue

    if not clicked:
        print("⚠️ No button clicked — sending ENTER")
        password.send_keys(Keys.RETURN)

    time.sleep(6)

    driver.save_screenshot("/tmp/ariba_post_login.png")
    print("Post-login URL:", driver.current_url)
    print("Post-login Title:", driver.title)

    # ── Close the Company Profile popup if it appears (optional) ──
    try:
        close_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[normalize-space(text())='Close'] | //button[@title='Close']")
            )
        )
        driver.execute_script("arguments[0].click();", close_btn)
        print("✓ Closed Company Profile popup")
        time.sleep(2)
    except:
        print("→ No popup, continuing...")

    driver.save_screenshot("/tmp/ariba_after_close_popup.png")
    print("After popup URL:", driver.current_url)

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
    # Pre-flight: check if Ariba is reachable before spinning up Chrome
    if not check_ariba_reachable():
        print("⚠️ Skipping Ariba search — endpoint unreachable from this runner.")
        print("   This is common on GitHub Actions due to Ariba's IP restrictions.")
        return []

    driver = build_driver(headless=True)
    wait = WebDriverWait(driver, 20)

    results = []

    try:
        ariba_login(driver, wait)

        for r in rfps:
            try:
                results.append(ariba_search_rfp(driver, wait, r))
            except Exception as e:
                print(f"⚠️ Failed to search RFP {r}: {e}")
                results.append({
                    "RFP No.": r,
                    "Lead Title": f"Error: {e}",
                    "Ariba URL": ""
                })

    except Exception as e:
        print(f"✗ Ariba session failed: {e}")

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

    if tender_data:
        write_to_sheet(
            get_or_create_worksheet(sheet, TENDER_ALERTS_SHEET),
            tender_data
        )
    else:
        print("⚠️ No tender data written — Ariba search returned no results.")

if __name__ == "__main__":
    main()

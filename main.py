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

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    os.environ["WDM_CACHE_PATH"] = "/tmp/wdm_cache"
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

    username = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter Username' or @name='UserName' or @id='UserName']")
    ))
    username.clear()
    username.send_keys(ARIBA_USERNAME)
    print("✓ Username entered")

    password = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@placeholder='Enter Password' or @type='password']")
    ))
    password.clear()
    password.send_keys(ARIBA_PASSWORD)
    print("✓ Password entered")

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
    with open("/tmp/ariba_post_login.html", "w") as f:
        f.write(driver.page_source)
    print("Post-login URL:", driver.current_url)
    print("Post-login Title:", driver.title)

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

def ariba_search_all_rfps(driver, wait, rfps):
    all_results = []
    seen = set()

    for rfp in rfps:
        print(f"→ Searching for {rfp}...")
        driver.get("https://service.ariba.com/Discovery.aw")
        time.sleep(3)

        # Dismiss cookie banner if present
        try:
            understood_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Understood')]"))
            )
            driver.execute_script("arguments[0].click();", understood_btn)
            time.sleep(1)
        except:
            pass

        # Type RFP into search bar and submit
        try:
            search_box = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH,
                    "//input[@placeholder='By Product' or contains(@placeholder,'Product') or contains(@aria-label,'Product')]"
                ))
            )
            search_box.clear()
            search_box.send_keys(rfp)
            time.sleep(1)
            search_box.send_keys(Keys.RETURN)
            print(f"✓ Search submitted for {rfp}")
        except Exception as e:
            print(f"⚠️ Search box not found for {rfp}: {e}")
            all_results.append({"RFI ID": "", "Lead Title": "Search box not found", "Respond By": "", "URL": ""})
            continue

        # Wait for results
        time.sleep(5)

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # ── Target result cards specifically by looking for the GPOR title link ──
        # Each card has a prominent <a> or <h2>/<h3> with the GPOR title
        # We find those anchors and then walk UP to the card container
        matched = False

        title_elements = soup.find_all(
            lambda tag: tag.name in ["a", "h2", "h3", "span", "div"]
            and rfp.upper() in tag.get_text(strip=True).upper()
            and len(tag.get_text(strip=True)) < 100  # exclude large containers
        )

        for title_el in title_elements:
            # Walk up to find the card container — stop at a sizeable parent
            card = title_el
            for _ in range(6):  # walk up max 6 levels
                parent = card.find_parent()
                if not parent:
                    break
                parent_text = parent.get_text(separator=" ", strip=True)
                # Stop when we have enough context (has RFI ID + Respond By fields)
                if re.search(r'RF[A-Z]\s*[·•]', parent_text) and 'Respond By' in parent_text:
                    card = parent
                    break
                card = parent

            card_text = re.sub(r'\s+', ' ', card.get_text(separator=" ", strip=True))

            dedup_key = card_text[:80]
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            # Extract RFI ID — anchored on "RFI ·" label
            rfi_id_match = re.search(r'RF[A-Z]\s*[·•]\s*(\S+)', card_text)
            rfi_id = rfi_id_match.group(1) if rfi_id_match else ""

            # Extract Lead Title — text before "RFI ·"
            title_match = re.match(r'^(.+?)\s+RF[A-Z]\s*[·•]', card_text)
            lead_title = title_match.group(1).strip() if title_match else title_el.get_text(strip=True)

            # ── Extract Respond By from sibling/adjacent elements ──
            # Look for the label and grab the NEXT sibling text node or element
            respond_by = ""

            # Try finding "Respond By" label element, then get adjacent value
            respond_label = card.find(
                lambda tag: tag.get_text(strip=True) in ["Respond By:", "Respond By"]
            )
            if respond_label:
                # Try next sibling
                sibling = respond_label.find_next_sibling()
                if sibling:
                    respond_by = sibling.get_text(strip=True)
                # If no sibling, try parent's next sibling
                if not respond_by and respond_label.parent:
                    next_parent = respond_label.parent.find_next_sibling()
                    if next_parent:
                        respond_by = next_parent.get_text(strip=True)

            # Fallback: regex on full card text
            if not respond_by:
                deadline_match = re.search(
                    r'Respond\s+By[:\s]+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}[\s,]*[\d:]*)',
                    card_text
                )
                respond_by = deadline_match.group(1).strip() if deadline_match else ""

            url = (
                f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
                f"comsapsbncdiscoveryui#/RfxEvent/preview/{rfi_id}"
                if rfi_id else ""
            )

            all_results.append({
                "RFI ID":     rfi_id,
                "Lead Title": lead_title,
                "Respond By": respond_by,
                "URL":        url
            })
            print(f"  ✓ {rfp} → RFI ID: {rfi_id}, Respond By: {respond_by}")
            matched = True
            break  # one result per RFP search — take the first/best match

        if not matched:
            print(f"  ⚠️ {rfp} not found")
            all_results.append({
                "RFI ID":     "",
                "Lead Title": "Not found",
                "Respond By": "",
                "URL":        ""
            })

    return all_results

# ---------------- CLEAN RESULTS ---------------- #

def clean_tender_data(results):
    JUNK_PATTERNS = [
        re.compile(r'^\d+\s+results\s+for', re.IGNORECASE),
        re.compile(r'^filters\s+clear\s+all', re.IGNORECASE),
        re.compile(r'^sort\s+by', re.IGNORECASE),
    ]

    def is_junk(title):
        return any(p.match(title.strip()) for p in JUNK_PATTERNS)

    cleaned = [r for r in results if not is_junk(r.get("Lead Title", ""))]

    best = {}
    for row in cleaned:
        key = row["RFI ID"] or row["Lead Title"]
        existing = best.get(key)
        if existing is None:
            best[key] = row
        elif existing["Lead Title"] == "Not found" and row["Lead Title"] != "Not found":
            best[key] = row

    return list(best.values())

# ---------------- MAIN ---------------- #

def run_ariba_search(rfps):
    if not check_ariba_reachable():
        print("⚠️ Skipping Ariba search — endpoint unreachable from this runner.")
        return []

    driver = build_driver(headless=True)
    wait = WebDriverWait(driver, 20)
    results = []

    try:
        ariba_login(driver, wait)
        results = ariba_search_all_rfps(driver, wait, rfps)
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
        tender_data = clean_tender_data(tender_data)
        write_to_sheet(
            get_or_create_worksheet(sheet, TENDER_ALERTS_SHEET),
            tender_data
        )
        print(f"✓ Written {len(tender_data)} rows to '{TENDER_ALERTS_SHEET}'")
    else:
        print("⚠️ No tender data written — Ariba search returned no results.")

if __name__ == "__main__":
    main()

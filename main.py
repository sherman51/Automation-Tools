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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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

ARIBA_LOGIN_URL = "https://service.ariba.com/Authenticator.aw/ad/ssoIDP"

# Set your Ariba credentials via environment variables:
#   ARIBA_USERNAME and ARIBA_PASSWORD
ARIBA_USERNAME = os.getenv("ARIBA_USERNAME", "")
ARIBA_PASSWORD = os.getenv("ARIBA_PASSWORD", "")

TENDER_ALERTS_SHEET = "Tender Alerts"

# ---------------- GOOGLE AUTH ---------------- #

def get_google_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise Exception("Missing GOOGLE_CREDENTIALS_JSON environment variable")
    creds_dict = json.loads(creds_json)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
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

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "table"]):
        if tag.name in ["h1", "h2", "h3", "h4"]:
            text = tag.get_text(strip=True)
            if any(month in text.lower() for month in [
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
                first_row_cols = rows[0].find_all(["td", "th"])
                headers = [c.get_text(strip=True) for c in first_row_cols]
                rows = rows[1:]

            for tr in rows:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                if not cols or cols == headers:
                    continue

                row = {"source_url": url}
                if current_month:
                    row["PERIOD"] = current_month

                for i in range(min(len(headers), len(cols))):
                    row[headers[i]] = cols[i]

                results.append(row)

    if not results:
        for item in soup.find_all("li"):
            text = item.get_text(" ", strip=True)
            if len(text) > 20:
                results.append({"source_url": url, "content": text})

    return results


def extract_rfp_numbers(data):
    """
    Extract all RFP numbers from scraped rows.
    Looks in dedicated 'RFP No.' columns AND scans all text fields
    for patterns like RFP-XXXX or RFPXXXXXXX.
    """
    rfp_set = set()
    rfp_pattern = re.compile(r'\bRFP[-\s]?\w+\b', re.IGNORECASE)

    for row in data:
        # Check dedicated column first
        for key in row:
            if "rfp" in key.lower() and "no" in key.lower():
                val = row[key].strip()
                if val:
                    rfp_set.add(val)

        # Also scan all values for embedded RFP numbers
        for val in row.values():
            if isinstance(val, str):
                matches = rfp_pattern.findall(val)
                for m in matches:
                    rfp_set.add(m.strip())

    return list(rfp_set)

# ---------------- GOOGLE SHEETS ---------------- #

def connect_spreadsheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
    return worksheet


def write_to_sheet(sheet, data):
    sheet.clear()
    if not data:
        print("No data found")
        return
    headers = list(data[0].keys())
    rows = [headers] + [[row.get(h, "") for h in headers] for row in data]
    sheet.update(rows)

# ---------------- ARIBA SELENIUM ---------------- #

def build_driver(headless=True):
    """Create a Chrome WebDriver. Set headless=False to watch it run."""
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    return driver


def ariba_login(driver, wait):
    """Log into Ariba - genuine two-step login (username → Next → password)."""
    print("  → Navigating to Ariba login...")
    driver.get(ARIBA_LOGIN_URL)
    time.sleep(5)

    driver.save_screenshot("/tmp/ariba_step1_login_page.png")
    print(f"  📸 Screenshot saved: ariba_step1_login_page.png")
    print(f"  📄 Page title: {driver.title}")
    print(f"  🌐 Current URL: {driver.current_url}")

    # --- Step 1: Enter username ---
    try:
        username_field = wait.until(EC.presence_of_element_located(
            (By.NAME, "userid")
        ))
        print("  ✓ Username field found")
    except Exception as e:
        driver.save_screenshot("/tmp/ariba_error_no_username.png")
        raise Exception(f"Could not find username field: {e}")

    username_field.clear()
    username_field.send_keys(ARIBA_USERNAME)
    print("  ✓ Username entered")

    # --- Step 2: Click the "Next" button ---
    # The button is a styled <input type="submit"> or <button> with value/text "Next"
    # Try multiple selectors in order of specificity
    next_clicked = False
    next_selectors = [
        (By.XPATH, "//input[@type='submit' and @value='Next']"),
        (By.XPATH, "//button[normalize-space(text())='Next']"),
        (By.XPATH, "//input[@type='submit']"),
        (By.XPATH, "//button[@type='submit']"),
        (By.XPATH, "//*[normalize-space(text())='Next']"),
    ]

    for by, selector in next_selectors:
        try:
            btn = wait.until(EC.element_to_be_clickable((by, selector)))
            # Scroll into view and click via JavaScript to bypass any overlay issues
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            driver.execute_script("arguments[0].click();", btn)
            next_clicked = True
            print(f"  ✓ Clicked Next button ({selector})")
            break
        except Exception:
            continue

    if not next_clicked:
        # Last resort: submit the form via JS
        try:
            driver.execute_script("document.querySelector('form').submit();")
            next_clicked = True
            print("  ✓ Submitted form via JavaScript (Next fallback)")
        except Exception as e:
            raise Exception(f"Could not click Next button: {e}")

    # --- Step 3: Wait for password field to appear ---
    # After clicking Next, Ariba dynamically shows the password field on the SAME page
    print("  ⏳ Waiting for password field to appear...")
    time.sleep(3)

    driver.save_screenshot("/tmp/ariba_step2_after_next.png")
    print(f"  📸 Screenshot saved: ariba_step2_after_next.png")
    print(f"  📄 Page title: {driver.title}")
    print(f"  🌐 Current URL: {driver.current_url}")

    try:
        password_field = wait.until(EC.visibility_of_element_located(
            (By.XPATH, "//input[@type='password']")
        ))
        print("  ✓ Password field appeared")
    except Exception:
        # Debug: print all inputs now visible
        inputs = driver.find_elements(By.TAG_NAME, "input")
        print(f"  🔍 Inputs after Next ({len(inputs)} total):")
        for inp in inputs:
            print(f"      type='{inp.get_attribute('type')}' name='{inp.get_attribute('name')}' id='{inp.get_attribute('id')}'")
        driver.save_screenshot("/tmp/ariba_error_no_password.png")
        raise Exception("Password field never appeared after clicking Next")

    password_field.clear()
    password_field.send_keys(ARIBA_PASSWORD)
    print("  ✓ Password entered")

    # --- Step 4: Click the Login/Sign In button ---
    login_clicked = False
    login_selectors = [
        (By.XPATH, "//input[@type='submit' and (contains(@value,'Log') or contains(@value,'Sign'))]"),
        (By.XPATH, "//button[contains(normalize-space(text()),'Log') or contains(normalize-space(text()),'Sign')]"),
        (By.XPATH, "//input[@type='submit']"),
        (By.XPATH, "//button[@type='submit']"),
    ]

    for by, selector in login_selectors:
        try:
            btn = wait.until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            driver.execute_script("arguments[0].click();", btn)
            login_clicked = True
            print(f"  ✓ Clicked Login button ({selector})")
            break
        except Exception:
            continue

    if not login_clicked:
        password_field.send_keys(Keys.RETURN)
        print("  ✓ Pressed Enter on password (Login fallback)")

    time.sleep(8)

    driver.save_screenshot("/tmp/ariba_step3_after_login.png")
    print(f"  📸 Screenshot saved: ariba_step3_after_login.png")
    print(f"  📄 Page title: {driver.title}")
    print(f"  🌐 Current URL: {driver.current_url}")

    # Verify we're no longer on the login page
    if "Authenticator" in driver.current_url or "login" in driver.current_url.lower():
        soup = BeautifulSoup(driver.page_source, "html.parser")
        errors = soup.find_all(attrs={"class": lambda c: c and "error" in c.lower()})
        err_text = " | ".join(e.get_text(strip=True) for e in errors if e.get_text(strip=True))
        raise Exception(f"Login failed — still on login page. Errors: {err_text or 'none visible'}")

    print("  ✓ Login complete")


def ariba_search_rfp(driver, wait, rfp_no):
    """
    Search for an RFP number on Ariba and return extracted fields.
    Returns a dict with all available fields, or None if not found.
    """
    print(f"  → Searching for: {rfp_no}")

    # Try direct search via URL parameter first
    search_url = f"https://service.ariba.com/Discovery.aw/ad/rfxList?rfxId={rfp_no}"
    driver.get(search_url)
    time.sleep(3)

    # If that doesn't work, use the search box
    try:
        search_box = driver.find_elements(
            By.XPATH,
            "//input[@type='search' or @placeholder or @name='searchTerms' or contains(@id,'search')]"
        )
        if search_box:
            search_box[0].clear()
            search_box[0].send_keys(rfp_no)
            search_box[0].send_keys(Keys.RETURN)
            time.sleep(3)
    except Exception:
        pass

    # Parse whatever page we landed on
    return parse_ariba_rfp_page(driver, rfp_no)


def parse_ariba_rfp_page(driver, rfp_no):
    """
    Extract RFI ID, Lead Title, Respond By Date and any other visible fields
    from the current Ariba page.
    """
    result = {
        "RFP No.": rfp_no,
        "RFI ID": "",
        "Lead Title": "",
        "Respond By Date": "",
        "Buyer": "",
        "Category": "",
        "Region": "",
        "Posted Date": "",
        "Description": "",
        "Ariba URL": driver.current_url,
    }

    soup = BeautifulSoup(driver.page_source, "html.parser")

    # --- Strategy 1: Look for labelled field pairs (label + value) ---
    label_map = {
        "rfi id":         "RFI ID",
        "rfp id":         "RFI ID",
        "event id":       "RFI ID",
        "title":          "Lead Title",
        "event title":    "Lead Title",
        "lead title":     "Lead Title",
        "respond by":     "Respond By Date",
        "response due":   "Respond By Date",
        "close date":     "Respond By Date",
        "deadline":       "Respond By Date",
        "buyer":          "Buyer",
        "organization":   "Buyer",
        "category":       "Category",
        "commodity":      "Category",
        "region":         "Region",
        "location":       "Region",
        "posted":         "Posted Date",
        "publish date":   "Posted Date",
        "description":    "Description",
    }

    # Search <th>/<td> pairs in tables
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower().rstrip(":")
                value = cells[1].get_text(strip=True)
                for key, field in label_map.items():
                    if key in label and value:
                        result[field] = value

    # Search <label> + sibling/following elements
    for label_tag in soup.find_all(["label", "dt", "span", "div"]):
        label_text = label_tag.get_text(strip=True).lower().rstrip(":")
        for key, field in label_map.items():
            if key in label_text:
                # Try next sibling
                sibling = label_tag.find_next_sibling()
                if sibling:
                    val = sibling.get_text(strip=True)
                    if val and not result[field]:
                        result[field] = val

    # --- Strategy 2: Try clicking into the first result row if on a list page ---
    if not result["RFI ID"] and not result["Lead Title"]:
        try:
            first_link = driver.find_element(
                By.XPATH,
                "//table//tr[2]//a | //div[contains(@class,'result')]//a | //td//a[contains(@href,'rfx') or contains(@href,'RFP')]"
            )
            first_link.click()
            time.sleep(3)
            # Re-parse the detail page
            soup2 = BeautifulSoup(driver.page_source, "html.parser")
            result["Ariba URL"] = driver.current_url

            for table in soup2.find_all("table"):
                for row in table.find_all("tr"):
                    cells = row.find_all(["th", "td"])
                    if len(cells) >= 2:
                        label = cells[0].get_text(strip=True).lower().rstrip(":")
                        value = cells[1].get_text(strip=True)
                        for key, field in label_map.items():
                            if key in label and value:
                                result[field] = value
        except Exception:
            pass

    # If still empty, at minimum store the page title as Lead Title
    if not result["Lead Title"]:
        title_tag = soup.find("title")
        if title_tag:
            result["Lead Title"] = title_tag.get_text(strip=True)

    found = any(result[f] for f in ["RFI ID", "Lead Title", "Respond By Date"])
    status = "✓ Found" if found else "✗ Not found"
    print(f"    {status}: {result['Lead Title'] or '(no title)'}")

    return result


def run_ariba_search(rfp_numbers):
    """
    Log into Ariba once, then search each RFP number.
    Returns list of result dicts.
    """
    if not rfp_numbers:
        print("No RFP numbers to search.")
        return []

    if not ARIBA_USERNAME or not ARIBA_PASSWORD:
        raise Exception(
            "Missing ARIBA_USERNAME or ARIBA_PASSWORD environment variables. "
            "Please set them before running."
        )

    print(f"\n🔍 Searching {len(rfp_numbers)} RFP number(s) on Ariba...")

    driver = build_driver(headless=True)
    wait = WebDriverWait(driver, 20)
    results = []

    try:
        ariba_login(driver, wait)

        for rfp_no in rfp_numbers:
            try:
                record = ariba_search_rfp(driver, wait, rfp_no)
                results.append(record)
                time.sleep(2)  # Be polite to the server
            except Exception as e:
                print(f"  ✗ Error searching {rfp_no}: {e}")
                results.append({
                    "RFP No.": rfp_no,
                    "RFI ID": "",
                    "Lead Title": f"ERROR: {e}",
                    "Respond By Date": "",
                    "Buyer": "",
                    "Category": "",
                    "Region": "",
                    "Posted Date": "",
                    "Description": "",
                    "Ariba URL": "",
                })

    finally:
        driver.quit()

    return results

# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_spreadsheet()
    pharma_data = []

    # Step 1 — Scrape ALPS Healthcare pages
    for url, sheet_name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")
        html = fetch(url)
        data = []

        if html:
            data = extract_events(html, url)

        print(f"  {sheet_name}: {len(data)} rows")

        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
        write_to_sheet(worksheet, data)

        # Collect pharma rows for Ariba search
        if "pharmaceutical" in sheet_name.lower():
            pharma_data = data

    print("✅ ALPS Healthcare sheets updated")

    # Step 2 — Extract RFP numbers from pharmaceutical sheet
    rfp_numbers = extract_rfp_numbers(pharma_data)
    print(f"\n📋 Found {len(rfp_numbers)} RFP number(s): {rfp_numbers}")

    # Step 3 — Search each RFP on Ariba
    tender_data = run_ariba_search(rfp_numbers)

    # Step 4 — Write results to Tender Alerts sheet
    if tender_data:
        tender_sheet = get_or_create_worksheet(spreadsheet, TENDER_ALERTS_SHEET)
        write_to_sheet(tender_sheet, tender_data)
        print(f"\n✅ Tender Alerts sheet updated with {len(tender_data)} record(s)")
    else:
        print("\n⚠️  No Ariba results to write")


if __name__ == "__main__":
    main()

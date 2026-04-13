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
    print("  → Navigating to Ariba login...")
    driver.get(ARIBA_LOGIN_URL)
    time.sleep(5)

    driver.save_screenshot("/tmp/ariba_step1_login_page.png")
    print(f"  📄 Page title: {driver.title}")
    print(f"  🌐 Current URL: {driver.current_url}")

    # --- Step 1: Enter username ---
    try:
        username_field = wait.until(EC.presence_of_element_located((By.NAME, "userid")))
        print("  ✓ Username field found")
    except Exception as e:
        driver.save_screenshot("/tmp/ariba_error_no_username.png")
        raise Exception(f"Could not find username field: {e}")

    username_field.clear()
    username_field.send_keys(ARIBA_USERNAME)
    print("  ✓ Username entered")

    # --- Step 2: Click Next ---
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
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            driver.execute_script("arguments[0].click();", btn)
            next_clicked = True
            print(f"  ✓ Clicked Next ({selector})")
            break
        except Exception:
            continue

    if not next_clicked:
        raise Exception("Could not click Next button")

    # --- Step 3: Wait and diagnose what appeared after Next ---
    # Give the SPA time to re-render
    time.sleep(5)
    driver.save_screenshot("/tmp/ariba_step2_after_next.png")
    print(f"  📸 Screenshot saved after Next click")
    print(f"  🌐 URL: {driver.current_url}")

    # Dump full page source for diagnosis
    page_source = driver.page_source
    with open("/tmp/ariba_page_after_next.html", "w") as f:
        f.write(page_source)
    print("  📄 Full page HTML saved: /tmp/ariba_page_after_next.html")

    # Check for iframes that appeared after Next
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    print(f"  🔍 Iframes found after Next: {len(iframes)}")
    for i, iframe in enumerate(iframes):
        src = iframe.get_attribute("src") or ""
        name = iframe.get_attribute("name") or ""
        print(f"      iframe[{i}]: src='{src}' name='{name}'")

    # Check for shadow DOM hosts
    shadow_hosts = driver.find_elements(By.CSS_SELECTOR, "*")
    shadow_count = 0
    for el in shadow_hosts[:50]:  # check first 50 elements
        try:
            shadow = driver.execute_script("return arguments[0].shadowRoot", el)
            if shadow:
                shadow_count += 1
                print(f"  🔍 Shadow DOM found on: <{el.tag_name} class='{el.get_attribute('class')}'>")
        except Exception:
            pass
    print(f"  🔍 Shadow DOM hosts found: {shadow_count}")

    # Print ALL visible inputs (not just hidden ones)
    inputs = driver.find_elements(By.TAG_NAME, "input")
    print(f"  🔍 All inputs after Next ({len(inputs)} total):")
    for inp in inputs:
        inp_type = inp.get_attribute('type')
        inp_name = inp.get_attribute('name')
        inp_id = inp.get_attribute('id')
        is_displayed = inp.is_displayed()
        print(f"      type='{inp_type}' name='{inp_name}' id='{inp_id}' visible={is_displayed}")

    # --- Step 4: Try to find password field in multiple ways ---
    password_field = None

    # Strategy A: Standard visible input
    try:
        password_field = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.XPATH, "//input[@type='password']"))
        )
        print("  ✓ Password found: standard visible input")
    except Exception:
        pass

    # Strategy B: Present in DOM but not visible — force-reveal via JS
    if not password_field:
        try:
            hidden_pw = driver.find_element(By.XPATH, "//input[@type='password']")
            driver.execute_script("""
                arguments[0].style.display = 'block';
                arguments[0].style.visibility = 'visible';
                arguments[0].style.opacity = '1';
                arguments[0].removeAttribute('hidden');
                arguments[0].removeAttribute('disabled');
            """, hidden_pw)
            password_field = hidden_pw
            print("  ✓ Password found: hidden in DOM, revealed via JS")
        except Exception:
            pass

    # Strategy C: Check inside iframes
    if not password_field:
        driver.switch_to.default_content()
        for i, iframe in enumerate(iframes):
            try:
                driver.switch_to.frame(iframe)
                pw = driver.find_element(By.XPATH, "//input[@type='password']")
                password_field = pw
                print(f"  ✓ Password found in iframe[{i}]")
                break
            except Exception:
                driver.switch_to.default_content()

    # Strategy D: Shadow DOM traversal
    if not password_field:
        driver.switch_to.default_content()
        try:
            pw = driver.execute_script("""
                function findPasswordInShadow(root) {
                    if (!root) return null;
                    var inputs = root.querySelectorAll('input[type="password"]');
                    if (inputs.length > 0) return inputs[0];
                    var allEls = root.querySelectorAll('*');
                    for (var el of allEls) {
                        if (el.shadowRoot) {
                            var found = findPasswordInShadow(el.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                }
                return findPasswordInShadow(document);
            """)
            if pw:
                password_field = pw
                print("  ✓ Password found via shadow DOM traversal")
        except Exception:
            pass

    if not password_field:
        driver.save_screenshot("/tmp/ariba_error_no_password.png")
        raise Exception("Password field never appeared after clicking Next — check /tmp/ariba_page_after_next.html")

    # --- Step 5: Enter password and submit ---
    driver.execute_script("arguments[0].value = arguments[1];", password_field, ARIBA_PASSWORD)
    driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", password_field)
    driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", password_field)
    print("  ✓ Password entered via JS value injection")

    # Click login button
    login_clicked = False
    login_selectors = [
        (By.XPATH, "//input[@type='submit' and (contains(@value,'Log') or contains(@value,'Sign') or contains(@value,'login'))]"),
        (By.XPATH, "//button[contains(normalize-space(text()),'Log') or contains(normalize-space(text()),'Sign')]"),
        (By.XPATH, "//input[@type='submit']"),
        (By.XPATH, "//button[@type='submit']"),
    ]
    for by, selector in login_selectors:
        try:
            btn = wait.until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].click();", btn)
            login_clicked = True
            print(f"  ✓ Clicked Login ({selector})")
            break
        except Exception:
            continue

    if not login_clicked:
        try:
            password_field.send_keys(Keys.RETURN)
            print("  ✓ Pressed Enter on password (fallback)")
        except Exception:
            driver.execute_script("document.querySelector('form').submit();")
            print("  ✓ Form submitted via JS (fallback)")

    time.sleep(8)
    driver.save_screenshot("/tmp/ariba_step3_after_login.png")
    print(f"  📸 Screenshot saved after login")
    print(f"  📄 Page title: {driver.title}")
    print(f"  🌐 Current URL: {driver.current_url}")
    print("  ✓ Login flow complete")


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

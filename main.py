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
    rows = [headers] + [[row.get(h, "") for h in headers] for row in data]
    sheet.update(rows)

# ---------------- KEYWORDS ---------------- #

def get_keywords_from_sheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet("KEYWORDS")
        records = ws.get_all_records()

        keywords = [
            (row.get("Keywords") or row.get("keywords") or "").strip().lower()
            for row in records
        ]

        keywords = [k for k in keywords if k]

        print(f"✓ Loaded {len(keywords)} keywords from KEYWORDS sheet")
        return keywords

    except Exception as e:
        print(f"⚠️ Could not load KEYWORDS sheet: {e}")
        return []

# ---------------- FILTER ---------------- #

def filter_relevant_leads(leads, keywords):
    if not leads:
        return []

    filtered = []

    for lead in leads:
        title = lead.get("Lead Title", "").lower()
        matched_term = lead.get("Matched Term", "").lower()
        text = f"{title} {matched_term}"

        if lead.get("Lead Title") == "Not found":
            filtered.append(lead)
            continue

        if any(kw in text for kw in keywords):
            filtered.append(lead)
            print(f"  ✓ Kept:    {lead['Lead Title'][:70]}")
        else:
            print(f"  ✗ Dropped: {lead['Lead Title'][:70]}")

    print(f"✓ Keyword filter: {len(leads)} → {len(filtered)} relevant")
    return filtered

# ---------------- ARIBA ---------------- #

def check_ariba_reachable():
    try:
        r = requests.get("https://service.ariba.com", headers=HEADERS, timeout=10)
        print(f"✓ Ariba reachable: HTTP {r.status_code}")
        return True
    except Exception as e:
        print(f"✗ Ariba not reachable: {e}")
        return False

def build_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def ariba_login(driver, wait):
    driver.get("https://service.ariba.com/Authenticator.aw")

    username = wait.until(EC.presence_of_element_located((By.NAME, "UserName")))
    username.send_keys(ARIBA_USERNAME)

    password = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='password']")))
    password.send_keys(ARIBA_PASSWORD)
    password.send_keys(Keys.RETURN)

    time.sleep(5)

def scroll_to_load_all(driver):
    last_count = 0
    stale_scrolls = 0

    while stale_scrolls < 4:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        driver.execute_script("""
            let containers = document.querySelectorAll('[class*="scroll"], [class*="list"], [class*="results"]');
            containers.forEach(el => el.scrollTop = el.scrollHeight);
        """)
        time.sleep(1)

        current_count = driver.execute_script("""
            return document.querySelectorAll('[class*="card"], [class*="lead"], [class*="result"]').length
        """)

        if current_count == last_count:
            stale_scrolls += 1
        else:
            stale_scrolls = 0
            last_count = current_count

def parse_cards(soup, search_terms):
    results = []
    seen_rfi_ids = set()

    title_elements = soup.find_all(
        lambda tag: tag.name in ["a","h2","h3","span","div"]
        and re.search(r'GPOR\s*\d+', tag.get_text(strip=True), re.IGNORECASE)
        and len(tag.get_text(strip=True)) < 100
    )

    for title_el in title_elements:
        card = title_el

        for _ in range(6):
            parent = card.find_parent()
            if not parent:
                break
            parent_text = parent.get_text(" ", strip=True)
            if re.search(r'RF[A-Z]\s*[·•]', parent_text) and 'Respond By' in parent_text:
                card = parent
                break
            card = parent

        text = re.sub(r'\s+', ' ', card.get_text(" ", strip=True))

        rfi_match = re.search(r'RF[A-Z]\s*[·•]\s*(\S+)', text)
        rfi_id = rfi_match.group(1) if rfi_match else ""

        if rfi_id in seen_rfi_ids:
            continue
        seen_rfi_ids.add(rfi_id)

        title_match = re.match(r'^(.+?)\s+RF[A-Z]', text)
        title = title_match.group(1) if title_match else title_el.get_text(strip=True)

        deadline_match = re.search(r'Respond\s+By[:\s]*(.*)', text)
        deadline = deadline_match.group(1) if deadline_match else ""

        url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/RfxEvent/preview/{rfi_id}" if rfi_id else ""

        matched_term = next((t for t in search_terms if t.lower() in text.lower()), "")

        results.append({
            "RFI ID": rfi_id,
            "Lead Title": title,
            "Respond By": deadline,
            "URL": url,
            "Matched Term": matched_term
        })

    return results

def ariba_search_all_rfps(driver, wait, search_terms):
    encoded = "%20".join(search_terms)
    url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/leads/search?commodityName={encoded}"

    driver.get(url)
    time.sleep(3)

    scroll_to_load_all(driver)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    return parse_cards(soup, search_terms)

# ---------------- MAIN ---------------- #

def run_ariba_search(search_terms):
    if not check_ariba_reachable():
        return []

    driver = build_driver()
    wait = WebDriverWait(driver, 20)

    try:
        ariba_login(driver, wait)
        results = ariba_search_all_rfps(driver, wait, search_terms)
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
    keywords = get_keywords_from_sheet(sheet)

    all_search_terms = list(dict.fromkeys(rfps + keywords))

    tender_data = run_ariba_search(all_search_terms)

    if tender_data:
        tender_data = filter_relevant_leads(tender_data, keywords)

        write_to_sheet(
            get_or_create_worksheet(sheet, TENDER_ALERTS_SHEET),
            tender_data
        )

        print(f"✓ Written {len(tender_data)} rows to '{TENDER_ALERTS_SHEET}'")

if __name__ == "__main__":
    main()

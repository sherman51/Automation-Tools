import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import re
import time
import numpy as np

from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- FREE AI ENGINE (LOCAL EMBEDDINGS) ---------------- #

from sentence_transformers import SentenceTransformer

# lightweight model (fast + good enough for procurement matching)
MODEL = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text):
    return MODEL.encode(text)

def cosine_similarity(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

def build_keyword_index(keywords):
    """Precompute keyword embeddings once"""
    return {kw: embed(kw) for kw in keywords}

def semantic_match(text, keyword_index):
    """Find best matching keyword using cosine similarity"""
    text_vec = embed(text)

    best_kw = None
    best_score = 0

    for kw, vec in keyword_index.items():
        score = cosine_similarity(text_vec, vec)

        if score > best_score:
            best_score = score
            best_kw = kw

    return best_kw, round(best_score, 3)

def enrich_lead_ai(lead, keyword_index):
    text = f"{lead.get('Lead Title','')} {lead.get('Matched Term','')}"

    if not text.strip():
        text = "unknown"

    kw, score = semantic_match(text, keyword_index)

    lead["AI_Matched_Keyword"] = kw
    lead["AI_Match_Score"] = score

    # simple classification layer
    t = text.lower()
    if any(x in t for x in ["drug", "pharma", "medicine", "vaccine", "clinical"]):
        lead["AI_Category"] = "Pharma"
    elif any(x in t for x in ["it", "software", "system", "cloud", "digital"]):
        lead["AI_Category"] = "IT"
    else:
        lead["AI_Category"] = "General"

    return lead, score

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

        print(f"✓ Loaded {len(keywords)} keywords")
        return keywords

    except Exception as e:
        print(f"⚠️ Could not load KEYWORDS sheet: {e}")
        return []

# ---------------- LEGACY FILTER (kept unchanged) ---------------- #

def filter_relevant_leads(leads, keywords):
    return leads

# ---------------- AI FILTER (CORE ENGINE) ---------------- #

def ai_filter_leads(leads, keyword_index, threshold=0.75):
    filtered = []

    for lead in leads:
        lead, score = enrich_lead_ai(lead, keyword_index)

        if score >= threshold or lead.get("Lead Title") == "Not found":
            filtered.append(lead)
            print(f"✓ KEEP ({score}): {lead['Lead Title'][:70]}")
        else:
            print(f"✗ DROP ({score}): {lead['Lead Title'][:70]}")

    print(f"AI Filtered: {len(filtered)}/{len(leads)} kept")
    return filtered

# ---------------- ARIBA (UNCHANGED) ---------------- #

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

    rfi_id_elements = soup.find_all(
        lambda tag: tag.name in ["span", "div", "a"]
        and re.fullmatch(r'\d{10}', tag.get_text(strip=True))
    )

    for id_el in rfi_id_elements:
        rfi_id = id_el.get_text(strip=True)
        if rfi_id in seen_rfi_ids:
            continue
        seen_rfi_ids.add(rfi_id)

        card = id_el
        for _ in range(8):
            parent = card.find_parent()
            if not parent:
                break
            if 'Respond By' in parent.get_text(" ", strip=True):
                card = parent
                break
            card = parent

        text = re.sub(r'\s+', ' ', card.get_text(" ", strip=True))

        title_match = re.match(r'^(.+?)\s+(?:RFI|RF[A-Z])\b', text)
        title = title_match.group(1).strip() if title_match else text[:80]

        deadline_match = re.search(r'Respond\s+By[:\s]*([\w,: ]+(?:AM|PM))', text)
        deadline = deadline_match.group(1).strip() if deadline_match else ""

        url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/RfxEvent/preview/{rfi_id}"

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

    keyword_index = build_keyword_index(keywords)

    search_terms = list(dict.fromkeys(rfps + keywords))

    tender_data = run_ariba_search(search_terms)

    if tender_data:
        tender_data = ai_filter_leads(tender_data, keyword_index, threshold=0.75)

        write_to_sheet(
            get_or_create_worksheet(sheet, TENDER_ALERTS_SHEET),
            tender_data
        )

        print(f"✓ Written {len(tender_data)} rows")

if __name__ == "__main__":
    main()

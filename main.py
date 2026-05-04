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
from selenium.webdriver.support.ui import WebDriverWait, Select as SeleniumSelect
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ---------------- AI ENGINE ---------------- #

from sentence_transformers import SentenceTransformer

MODEL = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text):
    return MODEL.encode(text)

def cosine_similarity(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

def build_keyword_index(keywords):
    return {kw: embed(kw) for kw in keywords}

def semantic_match(text, keyword_index):
    if not keyword_index:
        return None, 0.0

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
    text = f"{lead.get('Lead Title','')} {lead.get('Category','')} {lead.get('Matched Term','')}".strip()
    if not text:
        text = "unknown"

    kw, score = semantic_match(text, keyword_index)

    lead["AI_Matched_Keyword"] = kw
    lead["AI_Match_Score"] = score

    t = text.lower()
    if any(x in t for x in ["drug","pharma","vaccine","clinical","medical","hospital"]):
        lead["AI_Category"] = "Pharma/Medical"
    elif any(x in t for x in ["logistics","supply chain","warehouse","distribution","cold chain"]):
        lead["AI_Category"] = "Logistics"
    elif any(x in t for x in ["it","software","cloud","system","digital"]):
        lead["AI_Category"] = "IT"
    else:
        lead["AI_Category"] = "General"

    return lead, score

# ---------------- CONFIG ---------------- #

URL_SHEET_MAP = {
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/": "National Sourcing Events",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/": "Pharmaceutical Sourcing Events",
}

HEADERS = {"User-Agent": "Mozilla/5.0"}

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"
TENDER_ALERTS_SHEET = "Tender Alerts"

ARIBA_USERNAME = os.getenv("ARIBA_USERNAME", "")
ARIBA_PASSWORD = os.getenv("ARIBA_PASSWORD", "")

ALLOWED_LOCATIONS = ["singapore","sg"]

# ---------------- SCRAPER ---------------- #

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except:
        return ""

def extract_events(html, url):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    current_month = None

    for tag in soup.find_all(["h1","h2","h3","h4","table"]):
        if tag.name.startswith("h"):
            text = tag.get_text(strip=True)
            if any(m in text.lower() for m in ["january","february","march","april","may","june","july","august","september","october","november","december"]):
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

# ---------------- SHEETS ---------------- #

def get_creds():
    return Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")),
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    )

def connect():
    return gspread.authorize(get_creds()).open_by_key(SPREADSHEET_ID)

def get_ws(ss, name):
    try:
        return ss.worksheet(name)
    except:
        return ss.add_worksheet(title=name, rows="1000", cols="20")

def write(ws, data):
    ws.clear()
    if not data:
        return
    headers = list(data[0].keys())
    ws.update([headers] + [[r.get(h,"") for h in headers] for r in data])

# ---------------- KEYWORDS ---------------- #

def get_keywords(ss):
    try:
        ws = ss.worksheet("KEYWORDS")
        raw = [(r.get("Keywords") or "").strip() for r in ws.get_all_records() if r.get("Keywords")]

        kws = []
        for entry in raw:
            parts = re.split(r'[,;\n]+', entry)
            for p in parts:
                p = p.strip().lower()
                if p:
                    if len(p.split()) > 6:
                        kws.extend(p.split())
                    else:
                        kws.append(p)

        return list(dict.fromkeys(kws))
    except:
        return []

# ---------------- DRIVER ---------------- #

def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

def login(driver):
    driver.get("https://service.ariba.com/Authenticator.aw")

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.NAME,"UserName"))
    ).send_keys(ARIBA_USERNAME)

    driver.find_element(By.XPATH,"//input[@type='password']").send_keys(ARIBA_PASSWORD + Keys.RETURN)
    time.sleep(8)

# ---------------- FIXED HELPERS ---------------- #

def get_all_rfi_ids_on_page(driver):
    text = driver.execute_script("return document.body.innerText || '';")
    return frozenset(re.findall(r'RFI\s*[\u00b7\u2022\-|]\s*(\d+)', text))

def wait_for_page_change(driver, old_ids, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        new_ids = get_all_rfi_ids_on_page(driver)

        # ✅ FIXED logic
        if new_ids and not new_ids.issubset(old_ids):
            return True

        time.sleep(1)

    return False

def wait_for_loading(driver, timeout=30):
    try:
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, "[class*='sapUiLocalBusyIndicator']"))
        )
    except:
        pass

def set_page_size_50(driver):
    try:
        dropdowns = driver.find_elements(By.CSS_SELECTOR,"select")
        if dropdowns:
            SeleniumSelect(dropdowns[0]).select_by_visible_text("50")  # ✅ fixed
            time.sleep(3)
    except:
        pass

def is_singapore(location):
    if not location:
        return True
    return any(x in location.lower() for x in ALLOWED_LOCATIONS)

# ---------------- PARSER ---------------- #

def parse_ariba_cards(driver):
    text = driver.execute_script("return document.body.innerText || '';")

    cards = []
    pattern = re.compile(r'(RFI\s*[\u00b7\u2022\-|]\s*(\d+))')

    matches = list(pattern.finditer(text))

    for i, m in enumerate(matches):
        rfi_id = m.group(2)

        start = max(0, m.start() - 300)
        end = matches[i+1].start() if i+1 < len(matches) else m.end()+500
        block = text[start:end]

        lines = [l.strip() for l in block.split("\n") if l.strip()]

        title = lines[0] if lines else ""

        location = ""
        for line in lines:
            if line.lower().startswith("service locations:"):
                location = line.split(":",1)[1].strip()

        if len(title) < 10:
            continue

        if not is_singapore(location):
            continue

        cards.append({
            "RFI ID": rfi_id,
            "Lead Title": title,
            "Location": location
        })

    return cards

# ---------------- PAGINATION ---------------- #

def click_next(driver):
    buttons = driver.find_elements(By.CSS_SELECTOR,"button[aria-label*='Next']")
    for b in buttons:
        if b.is_displayed() and b.is_enabled():
            driver.execute_script("arguments[0].click();", b)
            return True
    return False

# ---------------- SEARCH ---------------- #

def search_ariba(driver, keyword_string):
    from urllib.parse import quote

    url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/leads/search?commodityName={quote(keyword_string)}&serviceLocations=Singapore"

    driver.get(url)
    time.sleep(5)

    set_page_size_50(driver)

    all_cards = []
    seen_ids = set()
    page = 1

    while True:
        print(f"Page {page}")

        # scroll for lazy load
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)

        cards = parse_ariba_cards(driver)

        new_cards = 0
        for c in cards:
            if c["RFI ID"] not in seen_ids:
                seen_ids.add(c["RFI ID"])
                all_cards.append(c)
                new_cards += 1

        print("New:", new_cards)

        # ✅ stop condition
        if new_cards == 0:
            break

        old_ids = get_all_rfi_ids_on_page(driver)

        if not click_next(driver):
            break

        wait_for_loading(driver)
        time.sleep(3)

        wait_for_page_change(driver, old_ids)

        page += 1

    return all_cards

def run_ariba(keyword_string):
    driver = build_driver()
    try:
        login(driver)
        return search_ariba(driver, keyword_string)
    finally:
        driver.quit()

# ---------------- AI FILTER ---------------- #

def ai_filter(leads, index, threshold=0.35):
    out = []
    for l in leads:
        l, score = enrich_lead_ai(l, index)
        if score >= threshold:
            out.append(l)
    return out

# ---------------- MAIN ---------------- #

def main():
    ss = connect()

    for url, name in URL_SHEET_MAP.items():
        write(get_ws(ss, name), extract_events(fetch(url), url))

    keywords = get_keywords(ss)
    keyword_string = " ".join(keywords[:20])

    raw = run_ariba(keyword_string)

    seen = set()
    dedup = []
    for r in raw:
        if r["RFI ID"] not in seen:
            seen.add(r["RFI ID"])
            dedup.append(r)

    index = build_keyword_index(keywords)
    final = ai_filter(dedup, index)

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)

if __name__ == "__main__":
    main()

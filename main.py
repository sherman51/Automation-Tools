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
    text = f"{lead.get('Lead Title','')} {lead.get('Matched Term','')}".strip()
    if not text:
        text = "unknown"

    kw, score = semantic_match(text, keyword_index)

    lead["AI_Matched_Keyword"] = kw
    lead["AI_Match_Score"] = score

    t = text.lower()
    if any(x in t for x in ["drug", "pharma", "vaccine", "clinical"]):
        lead["AI_Category"] = "Pharma"
    elif any(x in t for x in ["it", "software", "cloud", "system"]):
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

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"
TENDER_ALERTS_SHEET = "Tender Alerts"

ARIBA_USERNAME = os.getenv("ARIBA_USERNAME", "")
ARIBA_PASSWORD = os.getenv("ARIBA_PASSWORD", "")

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
    pattern = re.compile(r'\b(?:RFP|GPOR)[-\s]?\w+\b', re.IGNORECASE)
    out = set()

    for row in data:
        for v in row.values():
            if isinstance(v, str):
                for m in pattern.findall(v):
                    out.add(m.strip())

    return list(out)

# ---------------- SHEETS ---------------- #

def get_creds():
    return Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")),
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
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
    ws.update([headers] + [[r.get(h, "") for h in headers] for r in data])

# ---------------- KEYWORDS ---------------- #

def get_keywords(ss):
    try:
        ws = ss.worksheet("KEYWORDS")
        return [
            (r.get("Keywords") or "").strip().lower()
            for r in ws.get_all_records()
            if r.get("Keywords")
        ]
    except:
        return []

# ---------------- ARIBA FIXED ---------------- #

def build_driver():
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # Remove --single-process (crashes Chrome 114+)
    # Remove --no-zygote (conflicts with newer Chrome)
    # Remove --disable-software-rasterizer (unnecessary, causes issues)
    # Remove --remote-debugging-port (conflicts with WebDriver)

    # Add these for Chrome 147+ stability
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Use specific binary if available in CI
    # options.binary_location = "/usr/bin/google-chrome-stable"

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver

def login(driver):
    driver.get("https://service.ariba.com/Authenticator.aw")

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.NAME, "UserName"))
    ).send_keys(ARIBA_USERNAME)

    driver.find_element(By.XPATH, "//input[@type='password']").send_keys(
        ARIBA_PASSWORD + Keys.RETURN
    )

    time.sleep(8)

def scroll(driver):
    for _ in range(10):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

def parse_cards(html, terms):
    soup = BeautifulSoup(html, "html.parser")

    cards = soup.find_all(text=re.compile(r"\d{10}"))

    results = []
    for c in cards:
        parent = c.parent
        text = parent.get_text(" ", strip=True)

        rfi = c.strip()

        results.append({
            "RFI ID": rfi,
            "Lead Title": text[:120],
            "Matched Term": next((t for t in terms if t in text.lower()), "")
        })

    return results

def search_ariba(driver, terms):
    query = "%20".join(terms)
    url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/leads/search?commodityName={query}"

    driver.get(url)

    time.sleep(8)  # important wait

    scroll(driver)

    html = driver.page_source

    print("PAGE SIZE:", len(html))  # DEBUG

    return parse_cards(html, terms)

def run_ariba(terms):
    driver = build_driver()
    try:
        login(driver)
        return search_ariba(driver, terms)
    finally:
        driver.quit()

# ---------------- AI FILTER ---------------- #

def ai_filter(leads, index, threshold=0.55):
    out = []

    for l in leads:
        l, score = enrich_lead_ai(l, index)

        print("SCORE:", score, l["Lead Title"][:60])

        if score >= threshold:
            out.append(l)

    return out

# ---------------- MAIN ---------------- #

def main():
    ss = connect()

    pharma = []

    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)
        write(get_ws(ss, name), data)

        if "pharmaceutical" in name.lower():
            pharma = data

    rfps = extract_rfp_numbers(pharma)
    keywords = get_keywords(ss)

    terms = list(set(rfps + keywords))

    print("TERMS:", len(terms))

    raw = run_ariba(terms)

    print("RAW RESULTS:", len(raw))

    if not raw:
        print("❌ Ariba returned empty results")
        return

    index = build_keyword_index(keywords)

    final = ai_filter(raw, index)

    print("FINAL:", len(final))

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)

if __name__ == "__main__":
    main()

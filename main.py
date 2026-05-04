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
        kws = [
            (r.get("Keywords") or "").strip().lower()
            for r in ws.get_all_records()
            if r.get("Keywords")
        ]
        print(f"✅ Loaded {len(kws)} keywords: {kws[:5]}")
        return kws
    except Exception as e:
        print(f"⚠️ Could not load KEYWORDS sheet: {e}")
        return []

# ---------------- ARIBA ---------------- #

def build_driver():
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # Stability fixes for Chrome 147+ in CI
    # Removed: --single-process (crashes Chrome 114+)
    # Removed: --no-zygote (conflicts with newer Chrome)
    # Removed: --disable-software-rasterizer (causes issues)
    # Removed: --remote-debugging-port (conflicts with WebDriver protocol)
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

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
    results = []
    seen = set()

    # Match any ID-like token: uppercase letters/digits, 6+ chars
    id_pattern = re.compile(r'\b[A-Z0-9]{6,}\b')

    for el in soup.find_all(["div", "li", "tr", "span", "td", "article", "section"]):
        text = el.get_text(" ", strip=True)

        # Skip elements that are too short or too long to be a lead card
        if len(text) < 10 or len(text) > 2000:
            continue

        # Deduplicate by first 100 chars of text
        key = text[:100]
        if key in seen:
            continue
        seen.add(key)

        # Only keep elements that contain one of our search terms
        matched_term = next((t for t in terms if t.lower() in text.lower()), "")
        if not matched_term:
            continue

        # Extract first ID-like token as the lead ID
        ids = id_pattern.findall(text)
        rfi_id = ids[0] if ids else "N/A"

        results.append({
            "RFI ID": rfi_id,
            "Lead Title": text[:200],
            "Matched Term": matched_term
        })

    print(f"Found {len(results)} matching cards")

    # Debug: print page structure sample if nothing matched
    if not results:
        print("=== PAGE STRUCTURE SAMPLE (first 20 non-trivial elements) ===")
        count = 0
        for el in soup.find_all(["div", "li", "tr", "span"]):
            t = el.get_text(" ", strip=True)
            if len(t) > 80:
                print(" |", t[:120])
                count += 1
                if count >= 20:
                    break

    return results

def search_ariba(driver, terms):
    # Limit query length to avoid URL being too long
    query = "%20".join(terms[:10])
    url = f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/leads/search?commodityName={query}"

    driver.get(url)

    # Wait for JS-rendered cards rather than a blind sleep
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='card'], [class*='lead'], [class*='result'], [class*='item']"
            ))
        )
        print("✅ Content detected on page")
    except Exception:
        print("⚠️ Timed out waiting for card elements — proceeding anyway")

    scroll(driver)
    time.sleep(3)  # small buffer after scrolling

    html = driver.page_source
    print("PAGE SIZE:", len(html))

    # Save debug snapshot to inspect Ariba's actual HTML structure if needed
    try:
        with open("ariba_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("📄 Debug snapshot saved to ariba_debug.html")
    except Exception as e:
        print(f"Could not save debug snapshot: {e}")

    return parse_cards(html, terms)

def run_ariba(terms):
    driver = build_driver()
    try:
        # Sanity check: make sure driver is alive
        driver.get("about:blank")
        print("✅ Driver OK, title:", driver.title)

        login(driver)

        # Verify login succeeded
        current_url = driver.current_url
        page_title = driver.title
        print(f"Post-login URL: {current_url}")
        print(f"Post-login title: {page_title}")

        if "login" in current_url.lower() or "authenticat" in current_url.lower():
            print("❌ Login may have failed — still on auth page")
            return []

        return search_ariba(driver, terms)

    except Exception as e:
        print(f"❌ Ariba error: {e}")
        return []

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------------- AI FILTER ---------------- #

def ai_filter(leads, index, threshold=0.55):
    out = []

    for l in leads:
        title = l.get("Lead Title", "")

        # If no keyword index available, keep all matched leads as-is
        if not index:
            l["AI_Matched_Keyword"] = "fallback"
            l["AI_Match_Score"] = 1.0
            l["AI_Category"] = "General"
            out.append(l)
            print(f"  ✅ No index — keeping: {title[:60]}")
            continue

        l, score = enrich_lead_ai(l, index)
        print(f"  SCORE: {score} {title[:60]}")

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
    print(f"KEYWORD INDEX SIZE: {len(index)}")

    final = ai_filter(raw, index)

    print("FINAL:", len(final))

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)

if __name__ == "__main__":
    main()

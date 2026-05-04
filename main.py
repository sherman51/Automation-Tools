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

def scroll_to_load(driver):
    """Scroll incrementally to trigger lazy-loaded cards."""
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(20):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            print("  📄 Reached end of scroll")
            break
        last_height = new_height

def extract_cards_selenium(driver, terms):
    """
    Try to extract cards directly via Selenium using known SAP/Ariba class patterns.
    Returns a list of results, or None if no card elements were found (triggers fallback).
    """
    seen = set()
    results = []
    id_pattern = re.compile(r'\b[A-Z0-9]{6,}\b')

    # Common SAP BN / Fiori card class patterns — ordered by specificity
    selectors = [
        "[class*='leadCard']",
        "[class*='LeadCard']",
        "[class*='lead-card']",
        "[class*='sbn-lead']",
        "[class*='fd-card']",
        "[class*='sapMListItem']",
        "[class*='sapMLIB']",
        "[class*='opportunity']",
        "[class*='Opportunity']",
        "[class*='tileContent']",
        "[class*='TileContent']",
    ]

    elements = []
    matched_selector = None
    for sel in selectors:
        found = driver.find_elements(By.CSS_SELECTOR, sel)
        if found:
            print(f"  ✅ Found {len(found)} elements with selector: {sel}")
            elements = found
            matched_selector = sel
            break

    if not elements:
        print("  ⚠️ No card elements found via Selenium selectors")
        return None  # signal caller to fall back to BeautifulSoup

    print(f"  Using selector: {matched_selector}")

    for el in elements:
        try:
            text = el.text.strip()
        except Exception:
            continue

        # Skip blank, too short, or the results summary line
        if len(text) < 20:
            continue
        if re.search(r'^\d+\s+results?\s+for\b', text, re.IGNORECASE):
            continue

        matched_term = next((t for t in terms if t.lower() in text.lower()), "")
        if not matched_term:
            continue

        ids = id_pattern.findall(text)
        rfi_id = ids[0] if ids else "N/A"

        key = rfi_id if rfi_id != "N/A" else text[30:130]
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "RFI ID": rfi_id,
            "Lead Title": text[:200],
            "Matched Term": matched_term
        })

    print(f"  Found {len(results)} matching cards via Selenium")
    return results

def parse_cards_bs4(html, terms):
    """
    BeautifulSoup fallback parser.
    Only captures leaf-level elements (no matching children) to avoid
    capturing wrapper/container divs that swallow all child text.
    Skips the Ariba results summary line.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    id_pattern = re.compile(r'\b[A-Z0-9]{6,}\b')

    for el in soup.find_all(["div", "li", "tr", "span", "td", "article", "section"]):
        text = el.get_text(" ", strip=True)

        if len(text) < 20 or len(text) > 5000:
            continue

        # Skip the Ariba "X results for ..." summary line
        if re.search(r'^\d+\s+results?\s+for\b', text, re.IGNORECASE):
            continue

        matched_term = next((t for t in terms if t.lower() in text.lower()), "")
        if not matched_term:
            continue

        # Skip container elements — if a direct child also matches, this is a wrapper
        child_texts = [
            c.get_text(" ", strip=True)
            for c in el.find_all(["div", "li", "article", "section"], recursive=False)
        ]
        if any(
            len(ct) > 20 and any(t.lower() in ct.lower() for t in terms)
            for ct in child_texts
        ):
            continue

        ids = id_pattern.findall(text)
        rfi_id = ids[0] if ids else "N/A"

        key = rfi_id if rfi_id != "N/A" else text[30:130]
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "RFI ID": rfi_id,
            "Lead Title": text[:200],
            "Matched Term": matched_term
        })

    print(f"  Found {len(results)} matching cards via BeautifulSoup fallback")

    if not results:
        print("  === PAGE STRUCTURE SAMPLE (first 20 non-trivial elements) ===")
        count = 0
        for el in soup.find_all(["div", "li", "tr", "span"]):
            t = el.get_text(" ", strip=True)
            if len(t) > 80:
                print("  |", t[:120])
                count += 1
                if count >= 20:
                    break

    return results

def search_ariba(driver, terms, batch_size=5):
    all_results = []
    seen_keys = set()
    total_batches = (len(terms) + batch_size - 1) // batch_size

    for i in range(0, len(terms), batch_size):
        batch = terms[i:i+batch_size]
        batch_num = i // batch_size + 1
        query = "%20".join(batch)
        url = (
            f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
            f"comsapsbncdiscoveryui#/leads/search?commodityName={query}"
        )

        print(f"\n🔍 Batch {batch_num}/{total_batches}: {batch}")
        driver.get(url)

        # Wait for any recognisable card/list element to appear
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "[class*='leadCard'], [class*='LeadCard'], [class*='lead-card'], "
                    "[class*='sbn-lead'], [class*='fd-card'], [class*='sapMListItem'], "
                    "[class*='sapMLIB'], [class*='opportunity'], [class*='tileContent']"
                ))
            )
            print("  ✅ Card elements detected on page")
        except Exception:
            print("  ⚠️ Timed out waiting for card elements — proceeding anyway")

        scroll_to_load(driver)

        html = driver.page_source
        print(f"  PAGE SIZE: {len(html)}")

        # Try Selenium extraction first; fall back to BeautifulSoup
        batch_results = extract_cards_selenium(driver, terms)
        if batch_results is None:
            batch_results = parse_cards_bs4(html, terms)

        # Deduplicate across batches
        for r in batch_results:
            key = r["RFI ID"] if r["RFI ID"] != "N/A" else r["Lead Title"][30:130]
            if key not in seen_keys:
                seen_keys.add(key)
                all_results.append(r)

    # Save debug snapshot of last page
    try:
        with open("ariba_debug.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print("\n📄 Debug snapshot saved to ariba_debug.html")
    except Exception as e:
        print(f"Could not save debug snapshot: {e}")

    print(f"\n✅ Total unique results across all batches: {len(all_results)}")
    return all_results

def run_ariba(terms):
    driver = build_driver()
    try:
        driver.get("about:blank")
        print("✅ Driver OK, title:", driver.title)

        login(driver)

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

    # Scrape ALPS pages and write to their sheets (for reference)
    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)
        write(get_ws(ss, name), data)

    # Use only keywords for Ariba search
    keywords = get_keywords(ss)

    if not keywords:
        print("❌ No keywords found — check your KEYWORDS sheet has a 'Keywords' column with data")
        return

    terms = keywords
    print(f"TERMS (keywords only): {len(terms)}")

    raw = run_ariba(terms)

    print(f"\nRAW RESULTS: {len(raw)}")

    if not raw:
        print("❌ Ariba returned empty results")
        return

    index = build_keyword_index(terms)
    print(f"KEYWORD INDEX SIZE: {len(index)}")

    final = ai_filter(raw, index)

    print(f"FINAL: {len(final)}")

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")

if __name__ == "__main__":
    main()

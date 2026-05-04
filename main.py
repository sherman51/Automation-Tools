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
    text = f"{lead.get('Lead Title','')} {lead.get('Category','')} {lead.get('Matched Term','')}".strip()
    if not text:
        text = "unknown"

    kw, score = semantic_match(text, keyword_index)

    lead["AI_Matched_Keyword"] = kw
    lead["AI_Match_Score"] = score

    t = text.lower()
    if any(x in t for x in ["drug", "pharma", "vaccine", "clinical", "medical", "hospital"]):
        lead["AI_Category"] = "Pharma/Medical"
    elif any(x in t for x in ["logistics", "supply chain", "warehouse", "distribution", "cold chain"]):
        lead["AI_Category"] = "Logistics"
    elif any(x in t for x in ["it", "software", "cloud", "system", "digital"]):
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
        raw = [
            (r.get("Keywords") or "").strip()
            for r in ws.get_all_records()
            if r.get("Keywords")
        ]
        # Handle keywords stored as one long comma/semicolon/newline separated string
        kws = []
        for entry in raw:
            parts = re.split(r'[,;\n]+', entry)
            for p in parts:
                p = p.strip().lower()
                if p:
                    kws.append(p)
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
    for _ in range(30):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            print("  📄 Reached end of scroll")
            break
        last_height = new_height

def parse_ariba_cards(html):
    """
    Parse Ariba lead cards from the page HTML.

    Based on observed page structure, each card contains:
      - Title line (e.g. "NHGGO-RFI-GFN-FY25-369 NHG HEALTH Cluster Supply Chain Transformation")
      - Type + RFI number line (e.g. "RFI   ·   1110009226")
      - Service locations
      - Category
      - Max Budget
      - Respond By date

    We identify card boundaries by the "RFI · <number>" pattern and extract
    the block of text around each one.
    """
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text("\n", strip=True)

    cards = []

    # Split the full page text into blocks by the RFI·number pattern
    # Pattern: "RFI   ·   1110009226" or "RFI · 1110009226"
    rfi_pattern = re.compile(r'(RFI\s*[·•]\s*(\d{7,12}))', re.IGNORECASE)

    # Find all RFI markers and their positions
    matches = list(rfi_pattern.finditer(full_text))

    print(f"  Found {len(matches)} RFI markers in page text")

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

        # Grab text before this RFI marker (the title is just above it)
        start = max(0, match.start() - 300)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else match.end() + 500
        block = full_text[start:end].strip()

        # Extract lines from the block
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        # Title is typically the line just before "RFI · <number>"
        # Find the line index of the RFI marker
        rfi_line_text = match.group(1)
        title = ""
        category = ""
        location = ""
        budget = ""
        respond_by = ""

        for i, line in enumerate(lines):
            if rfi_pattern.search(line):
                # Title is the line before the RFI line
                if i > 0:
                    title = lines[i - 1]
                break

        # Extract structured fields from the block
        for line in lines:
            ll = line.lower()
            if ll.startswith("category:"):
                category = line[len("category:"):].strip()
            elif ll.startswith("service locations:"):
                location = line[len("service locations:"):].strip()
            elif ll.startswith("max budget:"):
                budget = line[len("max budget:"):].strip()
            elif ll.startswith("respond by:"):
                respond_by = line[len("respond by:"):].strip()

        if not title:
            continue

        cards.append({
            "RFI ID": rfi_id,
            "Lead Title": title,
            "Category": category,
            "Location": location,
            "Max Budget": budget,
            "Respond By": respond_by,
        })

    return cards

def search_ariba(driver, keyword_string):
    """
    Search Ariba with the full keyword string (as typed manually).
    Then paginate through all pages of results.
    """
    from urllib.parse import quote
    encoded = quote(keyword_string)
    url = (
        f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
        f"comsapsbncdiscoveryui#/leads/search?commodityName={encoded}"
    )

    print(f"\n🔍 Searching Ariba...")
    driver.get(url)

    # Wait for results to load
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='sapMListItem'], [class*='sapMLIB']"
            ))
        )
        print("  ✅ Results detected on page")
    except Exception:
        print("  ⚠️ Timed out waiting for results — proceeding anyway")

    all_cards = []
    seen_ids = set()
    page_num = 1

    while True:
        print(f"\n  📄 Scraping page {page_num}...")

        scroll_to_load(driver)
        time.sleep(2)

        html = driver.page_source
        print(f"  PAGE SIZE: {len(html)}")

        # Save debug snapshot
        try:
            with open(f"ariba_debug_p{page_num}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        cards = parse_ariba_cards(html)
        print(f"  Parsed {len(cards)} cards on page {page_num}")

        new_cards = 0
        for card in cards:
            if card["RFI ID"] not in seen_ids:
                seen_ids.add(card["RFI ID"])
                all_cards.append(card)
                new_cards += 1

        print(f"  {new_cards} new unique cards (total so far: {len(all_cards)})")

        if new_cards == 0:
            print("  No new cards — stopping pagination")
            break

        # Try to click the "Next" pagination button
        try:
            next_btn = driver.find_element(By.CSS_SELECTOR,
                "[class*='sapMPaginatorNext'], "
                "[class*='nextPage'], "
                "button[aria-label*='Next'], "
                "button[title*='Next'], "
                "a[aria-label*='Next']"
            )
            if next_btn.is_enabled() and next_btn.is_displayed():
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(5)
                page_num += 1
            else:
                print("  Next button disabled — last page reached")
                break
        except Exception:
            print("  No Next button found — single page or end of results")
            break

    print(f"\n✅ Total unique cards scraped: {len(all_cards)}")
    return all_cards

def run_ariba(keyword_string):
    driver = build_driver()
    try:
        driver.get("about:blank")
        print("✅ Driver OK")

        login(driver)

        current_url = driver.current_url
        print(f"Post-login URL: {current_url}")
        print(f"Post-login title: {driver.title}")

        if "login" in current_url.lower() or "authenticat" in current_url.lower():
            print("❌ Login may have failed — still on auth page")
            return []

        return search_ariba(driver, keyword_string)

    except Exception as e:
        print(f"❌ Ariba error: {e}")
        return []

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------------- AI FILTER ---------------- #

def ai_filter(leads, index, threshold=0.45):
    out = []

    for l in leads:
        title = l.get("Lead Title", "")
        category = l.get("Category", "")

        if not index:
            l["AI_Matched_Keyword"] = "fallback"
            l["AI_Match_Score"] = 1.0
            l["AI_Category"] = "General"
            out.append(l)
            print(f"  ✅ No index — keeping: {title[:60]}")
            continue

        l, score = enrich_lead_ai(l, index)
        print(f"  SCORE: {score} | {title[:60]} | {category[:40]}")

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

    # Load keywords
    keywords = get_keywords(ss)

    if not keywords:
        print("❌ No keywords found — check your KEYWORDS sheet has a 'Keywords' column with data")
        return

    # Join all keywords into one search string — exactly as you'd type in the search bar
    keyword_string = " ".join(keywords)
    print(f"\nSearch string ({len(keywords)} keywords): {keyword_string[:120]}...")

    # Run Ariba search and scrape all result cards
    raw = run_ariba(keyword_string)

    print(f"\nRAW RESULTS: {len(raw)}")

    if not raw:
        print("❌ Ariba returned empty results")
        return

    # Build semantic index and filter
    index = build_keyword_index(keywords)
    print(f"KEYWORD INDEX SIZE: {len(index)}")

    final = ai_filter(raw, index)

    print(f"FINAL: {len(final)}")

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")

if __name__ == "__main__":
    main()

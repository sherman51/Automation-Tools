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

ALLOWED_LOCATIONS = ["singapore", "sg"]

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

        seen = set()
        unique_kws = []
        for k in kws:
            if k not in seen:
                seen.add(k)
                unique_kws.append(k)

        print(f"✅ Loaded {len(unique_kws)} keywords: {unique_kws[:10]}")
        return unique_kws
    except Exception as e:
        print(f"⚠️ Could not load KEYWORDS sheet: {e}")
        return []

# ---------------- ARIBA DRIVER ---------------- #

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

# ---------------- PAGE SIZE (FIX 3) ---------------- #

def set_page_size_50(driver):
    """
    Set items-per-page using UI5-aware approach.
    UI5 renders options as custom list items in a DOM overlay popup,
    not as native <option> elements — so we handle both cases.
    """
    try:
        selectors = [
            "[class*='sapMSlt']",
            "select[id*='pageSize']",
            "select[id*='PerPage']",
            "[id*='pageSize']",
            "[aria-label*='Items per page']",
            "[aria-label*='items per page']",
        ]

        dropdown = None
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                dropdown = els[0]
                print(f"  📋 Found items-per-page control via: {sel}")
                break

        if not dropdown:
            print("  ⚠️  Could not find items-per-page dropdown — continuing with default")
            return

        driver.execute_script("arguments[0].scrollIntoView(true);", dropdown)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", dropdown)
        time.sleep(1.5)

        # Case 1: native <select> element
        if dropdown.tag_name.lower() == "select":
            from selenium.webdriver.support.ui import Select as SeleniumSelect
            for val in ["50", "100"]:
                try:
                    SeleniumSelect(dropdown).select_by_visible_text(val)
                    print(f"  ✅ Set page size to {val} (native select)")
                    time.sleep(3)
                    return
                except Exception:
                    continue

        # Case 2: UI5 custom select — options render in a page-level popup
        ui5_option_xpaths = [
            "//li[@role='option' and normalize-space(.)='50']",
            "//div[@role='option' and normalize-space(.)='50']",
            "//*[contains(@class,'sapMLIB') and normalize-space(.)='50']",
            "//*[contains(@class,'sapMSelectListItem') and normalize-space(.)='50']",
            "//li[normalize-space(text())='50']",
            "//span[normalize-space(text())='50']",
        ]
        for xpath in ui5_option_xpaths:
            opts = driver.find_elements(By.XPATH, xpath)
            if opts:
                driver.execute_script("arguments[0].click();", opts[0])
                print("  ✅ Set page size to 50 (UI5 list item)")
                time.sleep(3)
                return

        print("  ⚠️  Could not select page size option — continuing with default")

    except Exception as e:
        print(f"  ⚠️  set_page_size_50 error: {e} — continuing anyway")

# ---------------- PAGE NUMBER DETECTION (FIX 4) ---------------- #

def get_current_page_number(driver):
    """
    Extract current page number from UI5 pagination controls.
    Matches patterns like 'Page 2 of 11' or '2 / 11'.
    Returns (current_page, total_pages) or (None, None).
    """
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'(?:page\s+)?(\d+)\s*(?:of|/)\s*(\d+)', text, re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None, None

# ---------------- SPINNER DETECTION (FIX 2) ---------------- #

def wait_for_spinner_gone(driver, timeout=20):
    """
    Wait for SAP UI5 busy/loading indicators to disappear.
    UI5 shows a spinner overlay during navigation — detecting its
    disappearance is a reliable signal that the new page is ready.
    Returns True when clear (or if no spinner was ever found).
    """
    spinner_selectors = [
        "[class*='sapUiLocalBusy']",
        "[class*='sapUiBusy']",
        "[class*='sapMBusyDialog']",
        ".sapUiBlockLayerTabbable",
        "[class*='sapUiBlockLayer']",
    ]
    deadline = time.time() + timeout
    spinner_seen = False

    while time.time() < deadline:
        found = False
        for sel in spinner_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if any(e.is_displayed() for e in els):
                    found = True
                    spinner_seen = True
                    break
            except Exception:
                pass

        if spinner_seen and not found:
            print("  ✅ Spinner gone — page ready")
            return True
        if not found and not spinner_seen:
            # No spinner appeared at all — don't wait forever
            return True

        time.sleep(0.5)

    print("  ⚠️  Spinner wait timed out — proceeding anyway")
    return True  # Don't block pagination on spinner timeout

# ---------------- WAIT FOR PAGE CHANGE (FIX 1) ---------------- #

def wait_for_page_change(driver, old_ids, old_page_num=None, timeout=45):
    """
    Two-phase wait tuned for SAP UI5's virtual DOM patching.

    Phase 1: detect that DOM content cleared (transition started).
             UI5 wipes old nodes before inserting new ones — catching
             this transient empty state confirms the click registered.

    Phase 2: wait for new, non-empty content to appear.
             Also checks page number increment as a parallel signal.

    Falls back gracefully if phase 1 doesn't trigger within 8s.
    """
    # First: wait for UI5 spinner/busy overlay (most reliable signal)
    wait_for_spinner_gone(driver, timeout=15)

    deadline = time.time() + timeout

    # Phase 1 — detect DOM clearing (UI5 virtual patch in progress)
    phase1_deadline = time.time() + 8
    cleared = False
    while time.time() < phase1_deadline:
        try:
            current_ids = get_all_rfi_ids_on_page(driver)
            if not current_ids or (old_ids and not current_ids.intersection(old_ids)):
                cleared = True
                print("  ⏳ DOM clearing detected — waiting for new content...")
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not cleared:
        print("  ℹ️  Phase 1 skip (DOM didn't clear) — checking for content change")

    # Phase 2 — wait for new content to appear
    while time.time() < deadline:
        try:
            new_ids = get_all_rfi_ids_on_page(driver)

            # Signal A: RFI IDs changed
            if new_ids and new_ids != old_ids:
                print(f"  ✅ Page changed ({len(old_ids)} → {len(new_ids)} RFIs visible)")
                return True

            # Signal B: page number incremented (more reliable for sparse pages)
            if old_page_num is not None:
                new_page, _ = get_current_page_number(driver)
                if new_page and new_page != old_page_num:
                    print(f"  ✅ Page number advanced ({old_page_num} → {new_page})")
                    return True

        except Exception:
            pass
        time.sleep(1)

    print(f"  ⚠️  Page did not change within {timeout}s")
    return False

# ---------------- CARD PARSING ---------------- #

def get_all_rfi_ids_on_page(driver):
    """
    Return a frozenset of ALL RFI IDs currently visible on the page.
    Uses innerText to handle the middle-dot (U+00B7) between 'RFI' and the ID.
    """
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        pattern = re.compile(r'RFI\s*[\u00b7\u2022\-|]\s*(\d{7,12})', re.IGNORECASE)
        ids = frozenset(pattern.findall(text))
        return ids
    except Exception:
        return frozenset()


def is_singapore(location_str):
    """
    Return True if the location field refers to Singapore.
    Empty location is kept (don't silently drop leads with no location info).
    """
    if not location_str:
        return True
    loc = location_str.lower()
    return any(term in loc for term in ALLOWED_LOCATIONS)


def parse_ariba_cards(driver):
    """
    Parse Ariba lead cards from rendered innerText.
    Hard-filters to Singapore leads only.
    """
    try:
        full_text = driver.execute_script("return document.body.innerText || '';")
    except Exception:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        full_text = soup.get_text("\n", strip=True)

    cards = []
    rfi_pattern = re.compile(
        r'(RFI\s*[\u00b7\u2022\-|]\s*(\d{7,12}))',
        re.IGNORECASE
    )
    matches = list(rfi_pattern.finditer(full_text))
    print(f"  Found {len(matches)} RFI markers in page text")

    skipped_location = 0

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

        start = max(0, match.start() - 300)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else match.end() + 500
        block = full_text[start:end].strip()

        lines = [l.strip() for l in block.split("\n") if l.strip()]

        title = ""
        category = ""
        location = ""
        budget = ""
        respond_by = ""
        contract_length = ""
        decision_deadline = ""

        for i, line in enumerate(lines):
            if rfi_pattern.search(line):
                if i > 0:
                    title = lines[i - 1]
                break

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
            elif ll.startswith("contract length:"):
                contract_length = line[len("contract length:"):].strip()
            elif ll.startswith("decision deadline:"):
                decision_deadline = line[len("decision deadline:"):].strip()

        if not title or len(title) < 10:
            print(f"  ⚠️  Skipping invalid title: '{title}' (RFI {rfi_id})")
            continue

        if not is_singapore(location):
            print(f"  🌍 Skipping non-SG lead: '{title[:50]}' (location: {location})")
            skipped_location += 1
            continue

        cards.append({
            "RFI ID": rfi_id,
            "Lead Title": title,
            "Category": category,
            "Location": location,
            "Max Budget": budget,
            "Respond By": respond_by,
            "Contract Length": contract_length,
            "Decision Deadline": decision_deadline,
        })

    if skipped_location:
        print(f"  🌍 Skipped {skipped_location} non-Singapore cards on this page")

    return cards

# ---------------- PAGINATION ---------------- #

def click_next(driver):
    """
    Try all known Next-button selectors for Ariba's SAP UI5 interface.
    Scrolls the button into view before clicking.
    Returns True if a clickable Next button was found and clicked.
    """
    selectors = [
        "button[aria-label*='Next Page']",
        "button[aria-label*='Next']",
        "[class*='sapMPaginatorNext']",
        "[class*='nextPage']",
        "button[title*='Next']",
        "a[aria-label*='Next']",
        "[id*='nextPage']",
    ]
    for sel in selectors:
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            for btn in btns:
                if btn.is_displayed() and btn.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                    time.sleep(0.3)
                    driver.execute_script("arguments[0].click();", btn)
                    print(f"  ➡️  Clicked Next via: {sel}")
                    return True
        except Exception:
            continue
    return False

# ---------------- ARIBA SEARCH ---------------- #

def search_ariba(driver, keyword_string):
    """
    Search Ariba filtered to Singapore, paginate through ALL pages.

    Uses four-signal pagination detection (Fix 1-4):
      1. Two-phase DOM change detection (phase: clear → repopulate)
      2. UI5 spinner/busy overlay disappearance
      3. UI5 custom list item selector for page-size dropdown
      4. Page number increment as a parallel confirmation signal
    """
    from urllib.parse import quote

    encoded_kw = quote(keyword_string)
    encoded_loc = quote("Singapore")

    url = (
        f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
        f"comsapsbncdiscoveryui#/leads/search"
        f"?commodityName={encoded_kw}"
        f"&serviceLocations={encoded_loc}"
    )

    print(f"\n🔍 Searching Ariba (Singapore only)...")
    driver.get(url)

    # Wait for initial results to load
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='sapMListItem'], [class*='sapMLIB']"
            ))
        )
        print("  ✅ Results detected on page")
    except Exception:
        print("  ⚠️  Timed out waiting for results — proceeding anyway")

    # Give UI5 a moment to fully render before we try to change page size
    time.sleep(2)

    print("\n  ⚙️  Setting items per page...")
    set_page_size_50(driver)

    all_cards = []
    seen_ids = set()
    page_num = 1
    consecutive_empty = 0  # Guard against infinite loops on empty pages

    while True:
        print(f"\n  📄 Scraping page {page_num}...")
        time.sleep(3)

        # Save debug snapshot for troubleshooting
        try:
            with open(f"ariba_debug_p{page_num}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass

        cards = parse_ariba_cards(driver)
        print(f"  Parsed {len(cards)} Singapore cards on page {page_num}")

        if not cards:
            consecutive_empty += 1
            print(f"  ⚠️  No cards on page {page_num} ({consecutive_empty} consecutive empty)")
            if consecutive_empty >= 2:
                print("  ⏹  Two consecutive empty pages — stopping")
                break
        else:
            consecutive_empty = 0

        new_cards = 0
        for card in cards:
            if card["RFI ID"] not in seen_ids:
                seen_ids.add(card["RFI ID"])
                all_cards.append(card)
                new_cards += 1

        print(f"  {new_cards} new unique cards added (total so far: {len(all_cards)})")

        # Capture current state BEFORE clicking Next (for change detection)
        ids_this_page = get_all_rfi_ids_on_page(driver)
        current_page_num, total_pages = get_current_page_number(driver)

        if current_page_num and total_pages:
            print(f"  📊 Page {current_page_num} of {total_pages}")
            if current_page_num >= total_pages:
                print("  ⏹  Reached last page (page number check)")
                break

        clicked = click_next(driver)
        if not clicked:
            print("  ⏹  No Next button found — reached last page")
            break

        # Wait for page to actually change (all four signals)
        changed = wait_for_page_change(
            driver,
            old_ids=ids_this_page,
            old_page_num=current_page_num,
            timeout=45
        )

        if not changed:
            print("  ⏹  Page did not change after Next click — stopping")
            break

        page_num += 1

    print(f"\n✅ Total Singapore cards scraped: {len(all_cards)}")
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

def ai_filter(leads, index, threshold=0.35):
    """
    Semantic similarity filter.
    Threshold 0.35 catches relevant leads like logistics/supply chain (~0.39).
    All leads at this point are already Singapore-only.
    """
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

    # Scrape ALPS pages
    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)
        write(get_ws(ss, name), data)

    # Load keywords
    keywords = get_keywords(ss)

    if not keywords:
        print("❌ No keywords found — check your KEYWORDS sheet has a 'Keywords' column with data")
        return

    # Use first 20 keywords for search URL (avoid URL length limits)
    keyword_string = " ".join(keywords[:20])
    print(f"\nSearch string ({len(keywords)} keywords): {keyword_string[:120]}...")

    raw = run_ariba(keyword_string)

    print(f"\nRAW RESULTS (before dedup): {len(raw)}")

    # Deduplicate by RFI ID
    seen = set()
    deduped = []
    for r in raw:
        rid = r.get("RFI ID", "")
        if rid and rid not in seen:
            seen.add(rid)
            deduped.append(r)
        elif not rid:
            deduped.append(r)

    print(f"AFTER DEDUP: {len(deduped)}")

    if not deduped:
        print("❌ Ariba returned empty results")
        return

    # Build semantic index and filter
    index = build_keyword_index(keywords)
    print(f"KEYWORD INDEX SIZE: {len(index)}")

    final = ai_filter(deduped, index)

    print(f"FINAL: {len(final)}")

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")

if __name__ == "__main__":
    main()

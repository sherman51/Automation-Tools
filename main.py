import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import re
import time
import numpy as np
from urllib.parse import quote

from google.oauth2.service_account import Credentials

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
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
    text = f"{lead.get('Lead Title','')} {lead.get('Category','')}".strip()
    if not text:
        text = "unknown"
    kw, score = semantic_match(text, keyword_index)
    lead["Matched_Keyword"] = kw
    lead["Match_Score"] = score
    t = text.lower()
    if any(x in t for x in ["drug", "pharma", "vaccine", "clinical", "medical", "hospital"]):
        lead["Match_Category"] = "Pharma/Medical"
    elif any(x in t for x in ["logistics", "supply chain", "warehouse", "distribution", "cold chain"]):
        lead["Match_Category"] = "Logistics"
    elif any(x in t for x in ["it", "software", "cloud", "system", "digital"]):
        lead["Match_Category"] = "IT"
    else:
        lead["Match_Category"] = "General"
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

# ---------------- ALPS SCRAPER ---------------- #

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
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "table"]):
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
                headers = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
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

# ---------------- GOOGLE SHEETS ---------------- #

def get_creds():
    return Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON")),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
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

# ---------------- SELENIUM DRIVER ---------------- #

def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # 1280x900 keeps the pagination bar inside the headless click region.
    options.add_argument("--window-size=1280,900")
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

# ---------------- UI5 DIAGNOSTICS ---------------- #

def check_ui5_available(driver):
    """Log whether sap.ui is loaded and how many controls are registered."""
    result = driver.execute_script("""
        try {
            var v = sap.ui.version;
            var els = sap.ui.getCore().mElements || sap.ui.getCore()._mElements || {};
            return 'UI5 v' + v + ', ' + Object.keys(els).length + ' controls registered';
        } catch(e) {
            return 'unavailable: ' + e.message;
        }
    """)
    print(f"  🔧 UI5 check: {result}")
    return "unavailable" not in result

# ---------------- PAGE SIZE ---------------- #

def set_page_size(driver):
    """
    FIX: Only try 50 and 25 — Ariba Discovery max is 50 items/page, not 100.
    Open the UI5 sapMSlt items-per-page dropdown and select the largest
    available option (50 -> 25).
    """
    try:
        controls = driver.find_elements(By.CSS_SELECTOR, "[class*='sapMSlt']")
        if not controls:
            print("  ⚠️  No page-size control found — using default")
            return

        ctrl = controls[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ctrl)
        time.sleep(0.5)

        # Open via UI5 API if possible, else plain DOM click
        driver.execute_script("""
            try {
                var c = sap.ui.getCore().byId(arguments[0].id);
                if (c && typeof c.open === 'function') { c.open(); return; }
            } catch(e) {}
            arguments[0].click();
        """, ctrl)
        time.sleep(1.5)  # wait for UI5 popup animation

        # FIX: Removed "100" — Ariba max page size is 50
        for val in ["50", "25"]:
            opts = driver.find_elements(By.XPATH,
                f"//*[@role='option' and contains(normalize-space(.), '{val}')]"
            )
            if not opts:
                opts = driver.find_elements(By.XPATH,
                    f"//*[contains(@class,'sapMSelectListItem') and normalize-space(text())='{val}']"
                )
            if opts:
                driver.execute_script("arguments[0].click();", opts[0])
                print(f"  ✅ Page size set to {val}")
                time.sleep(2)
                return

        # Popup open but option not found — close cleanly
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        print("  ⚠️  Page size option not found — using default")

    except Exception as e:
        print(f"  ⚠️  set_page_size error: {e}")

# ---------------- SEARCH BOX ---------------- #

def type_into_search(driver, keyword_string):
    """
    Type the full keyword string into Ariba's search box exactly as a
    human would. Ariba OR-matches every word so pasting the full string
    returns every lead containing any keyword.
    """
    search_selectors = [
        "input[placeholder*='Search']",
        "input[placeholder*='search']",
        "input[aria-label*='Search']",
        "input[aria-label*='search']",
        "[class*='sapMSFI']",
        "[class*='sapMInputBaseInner']",
        "input[type='search']",
        "input[id*='search']",
        "input[id*='Search']",
    ]

    inp = None
    for sel in search_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    inp = el
                    print(f"  🔎 Found search box via: {sel}")
                    break
        except Exception:
            continue
        if inp:
            break

    if not inp:
        print("  ⚠️  Search box not found — continuing with Singapore filter only")
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        time.sleep(0.3)
        ActionChains(driver).move_to_element(inp).click().perform()
        time.sleep(0.3)
        inp.clear()
        inp.send_keys(keyword_string)
        print(f"  🔎 Typed: '{keyword_string[:100]}{'...' if len(keyword_string) > 100 else ''}'")
        time.sleep(0.5)
        inp.send_keys(Keys.RETURN)
        print("  🔎 Search submitted — waiting for results...")
        time.sleep(4)
        return True
    except Exception as e:
        print(f"  ⚠️  Search box interaction failed: {e}")
        return False

# ---------------- PAGE STATE HELPERS ---------------- #

def get_all_rfi_ids(driver):
    """Return frozenset of all RFI IDs currently rendered on the page."""
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        pattern = re.compile(r'RFI\s*[\u00b7\u2022\-|]\s*(\d{7,12})', re.IGNORECASE)
        return frozenset(pattern.findall(text))
    except Exception:
        return frozenset()

def get_page_numbers(driver):
    """
    Extract (current_page, total_pages) from UI5 pagination text.
    Ariba renders this as '1 of 12' or '1/12'.
    Returns (None, None) if not found.
    """
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'(?:page\s+)?(\d+)\s*(?:of|/)\s*(\d+)', text, re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None, None

def get_content_fingerprint(driver):
    """Short fingerprint of current page content for change detection."""
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'RFI\s*[\u00b7\u2022\-|]\s*\d{7,12}', text, re.IGNORECASE)
        return text[m.start(): m.start() + 400] if m else text[:400]
    except Exception:
        return ""

def is_next_disabled(driver):
    """Return True if the Next Page button exists and is aria-disabled."""
    try:
        btns = driver.find_elements(By.CSS_SELECTOR,
            "button[aria-label*='Next Page'], button[aria-label*='Next']")
        for btn in btns:
            if btn.get_attribute("aria-disabled") in ("true", "True"):
                return True
        return False
    except Exception:
        return False

# ---------------- DEBUG BUTTONS ---------------- #

def debug_buttons(driver):
    """
    Print all visible buttons on the page to help diagnose pagination issues.
    Call this when Next click strategies are failing.
    """
    try:
        info = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            var out = [];
            btns.forEach(function(b) {
                var label = (b.getAttribute('aria-label') || b.textContent || '').trim();
                if (label) {
                    out.push(b.id + ' | ' + label.substring(0,50)
                             + ' | disabled:' + b.getAttribute('aria-disabled'));
                }
            });
            return out.join('\\n');
        """)
        print(f"  🔬 DEBUG — buttons on page:\n{info[:2000]}")
    except Exception as e:
        print(f"  🔬 DEBUG buttons error: {e}")

# ---------------- CLICK NEXT ---------------- #

def click_next(driver):
    """
    Click the Next Page button using UI5-native firePress() first,
    then fall back through DOM strategies.

    FIX: Extended button ID scan range from 60 → 500 to handle deep
    pagination (IDs increment by ~9 per page; page 42 needs ~__button420).
    FIX: Strategy 3 now attempts UI5 firePress() on the DOM element's ID
    before falling back to a plain .click(), which often fails in UI5 apps.
    FIX: Added debug_buttons() call when all strategies are exhausted.
    """

    # ── Strategy 1: scan all registered UI5 controls for Next button ──
    result = driver.execute_script("""
        try {
            var core = sap.ui.getCore();
            var els = core.mElements || core._mElements || {};
            for (var id in els) {
                var el = els[id];
                if (!el) continue;
                try {
                    var dom = el.getDomRef ? el.getDomRef() : null;
                    if (!dom || dom.tagName !== 'BUTTON') continue;
                    var label = dom.getAttribute('aria-label') || '';
                    var title  = dom.getAttribute('title') || '';
                    if (label.indexOf('Next') !== -1 || title.indexOf('Next') !== -1) {
                        if (dom.getAttribute('aria-disabled') === 'true') return 'disabled';
                        if (typeof el.firePress === 'function') {
                            el.firePress();
                            return 'firePress:' + id;
                        }
                    }
                } catch(inner) {}
            }
        } catch(e) {}
        return null;
    """)

    if result == 'disabled':
        print("  ⏹  [S1] Next button disabled — last page")
        return False
    if result:
        print(f"  ➡️  [S1] UI5 firePress — {result}")
        return True

    # ── Strategy 2: firePress by scanning Ariba button ID range ──
    # FIX: Extended upper bound from 60 → 500 to handle 40+ pages
    # (IDs increment ~9 per page: page 42 needs ~__button420)
    result = driver.execute_script("""
        try {
            var core = sap.ui.getCore();
            for (var n = 25; n <= 500; n++) {
                var ctrl = core.byId('__button' + n);
                if (!ctrl) continue;
                var dom = ctrl.getDomRef ? ctrl.getDomRef() : null;
                if (!dom) continue;
                var label = dom.getAttribute('aria-label') || '';
                if (label.indexOf('Next') !== -1) {
                    if (dom.getAttribute('aria-disabled') === 'true') return 'disabled';
                    if (typeof ctrl.firePress === 'function') {
                        ctrl.firePress();
                        return 'firePress-id:__button' + n + '|label:' + label;
                    }
                }
            }
        } catch(e) {}
        return null;
    """)

    if result == 'disabled':
        print("  ⏹  [S2] Next button disabled — last page")
        return False
    if result:
        print(f"  ➡️  [S2] UI5 firePress by ID — {result}")
        return True

    # ── Strategy 3: last button inside the pagination wrapper ──
    # FIX: Try UI5 firePress() on the DOM element's own ID first
    # before falling back to .click(), which UI5 often ignores
    result = driver.execute_script("""
        try {
            var wrapper = document.querySelector(
                '.discovery-pagination-wrapper, [class*="pagination"]'
            );
            if (wrapper) {
                var btns = wrapper.querySelectorAll('button');
                var last = btns[btns.length - 1];
                if (last && last.getAttribute('aria-disabled') === 'true') return 'disabled';
                if (last && !last.disabled) {
                    var domId = last.id;
                    if (domId) {
                        try {
                            var ctrl = sap.ui.getCore().byId(domId);
                            if (ctrl && typeof ctrl.firePress === 'function') {
                                ctrl.firePress();
                                return 'ui5-firePress-from-wrapper:' + domId;
                            }
                        } catch(e) {}
                    }
                    last.click();
                    return 'dom-last-in-wrapper';
                }
            }
        } catch(e) {}
        return null;
    """)

    if result == 'disabled':
        print("  ⏹  [S3] Next button disabled — last page")
        return False
    if result:
        print(f"  ➡️  [S3] DOM last-button-in-wrapper — {result}")
        return True

    # ── Strategy 4: dispatchEvent directly on the button element ──
    result = driver.execute_script("""
        try {
            var btns = document.querySelectorAll(
                "button[aria-label*='Next Page'], button[aria-label*='Next']"
            );
            for (var i = 0; i < btns.length; i++) {
                var btn = btns[i];
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                ['mousedown','mouseup','click'].forEach(function(t) {
                    btn.dispatchEvent(new MouseEvent(t, {
                        bubbles: true, cancelable: true, view: window
                    }));
                });
                return 'dispatchEvent:' + (btn.getAttribute('aria-label') || btn.id);
            }
        } catch(e) {}
        return null;
    """)

    if result:
        print(f"  ➡️  [S4] dispatchEvent — {result}")
        return True

    # ── Strategy 5: ActionChains after scrollIntoView ──
    try:
        btn = None
        for sel in ["button[aria-label*='Next Page']", "button[aria-label*='Next']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    if el.get_attribute("aria-disabled") not in ("true", "True"):
                        btn = el
                        break
            if btn:
                break

        if btn:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});", btn
            )
            time.sleep(0.5)
            ActionChains(driver).move_to_element(btn).pause(0.3).click().perform()
            print("  ➡️  [S5] ActionChains click")
            return True
    except Exception as e:
        print(f"  ⚠️  [S5] ActionChains failed: {e}")

    # FIX: Dump all buttons to logs so we can diagnose future failures
    print("  ❌ All Next click strategies exhausted")
    debug_buttons(driver)
    return False

# ---------------- WAIT FOR PAGE CHANGE ---------------- #

def wait_for_next_page(driver, old_ids, old_fingerprint, timeout=60):
    """
    Poll until the page content changes after clicking Next.
    FIX: Default timeout increased from 30s → 60s. Ariba's XHR can be
    slow on large paginated result sets.

    Two independent signals:
      A) RFI ID frozenset differs from the previous page
      B) Content fingerprint (first 400 chars around first card) differs
    Also exits early if Next button becomes disabled (just loaded last page).
    """
    time.sleep(2)  # let UI5 start its XHR / re-render cycle

    deadline = time.time() + timeout
    poll = 0

    while time.time() < deadline:
        time.sleep(1)
        poll += 1

        try:
            new_ids = get_all_rfi_ids(driver)
            new_fp  = get_content_fingerprint(driver)

            if new_ids and new_ids != old_ids:
                print(f"  ✅ Page changed (RFI set) after {poll}s")
                return True

            if new_fp and new_fp != old_fingerprint:
                print(f"  ✅ Page changed (content fingerprint) after {poll}s")
                return True

            # Next became disabled — last page content is now loaded
            if is_next_disabled(driver):
                print("  ⏹  Next button disabled — scraping final page")
                return True

            if poll % 5 == 0:
                print(f"    [poll {poll}s] waiting... RFIs visible: {len(new_ids)}")

        except Exception as e:
            print(f"    [poll {poll}s] error: {e}")

    print(f"  ⚠️  No change after {timeout}s — stopping")
    return False

# ---------------- CARD PARSING ---------------- #

def is_singapore(location_str):
    """
    Return True if location is Singapore or blank.
    Blank is kept because many SG tenders don't populate the location field.
    The URL filter already handles server-side narrowing.
    """
    if not location_str:
        return True
    return any(term in location_str.lower() for term in ALLOWED_LOCATIONS)


def parse_ariba_cards(driver):
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
    print(f"  Found {len(matches)} RFI markers on page")

    skipped_location = 0

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

        # FIX: Start the block at the PREVIOUS card's RFI match end (or
        # a small 50-char lookback) — NOT 300 chars back.
        # This prevents the previous card's footer lines from being
        # mistaken for this card's title.
        if idx > 0:
            prev_end = matches[idx - 1].end()
            start = prev_end  # start right after previous RFI marker
        else:
            start = max(0, match.start() - 400)  # first card: look back for title

        end = (matches[idx + 1].start()
               if idx + 1 < len(matches)
               else match.end() + 600)

        block = full_text[start:end].strip()
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        title = category = location = budget = ""
        respond_by = contract_length = decision_deadline = ""

        # FIX: Title must come from lines BEFORE the RFI marker,
        # but only within this card's own block (not bled from previous card).
        # Also skip lines that look like field labels from a prior card.
        field_prefixes = (
            "category:", "service locations:", "max budget:",
            "respond by:", "contract length:", "decision deadline:",
            "rfi", "rfp", "rfq"
        )
        for i, line in enumerate(lines):
            if rfi_pattern.search(line):
                # Walk backwards from RFI line to find the real title —
                # skip any lines that are clearly field values/labels
                for j in range(i - 1, -1, -1):
                    candidate = lines[j].strip()
                    if not candidate:
                        continue
                    if candidate.lower().startswith(field_prefixes):
                        continue
                    # Must be at least 10 chars and not purely numeric
                    if len(candidate) >= 10 and not candidate.replace(" ", "").isdigit():
                        title = candidate
                        break
                break

        # Structured fields follow the RFI line
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
            print(f"  🌍 Skipping non-SG: '{title[:50]}' (location: '{location}')")
            skipped_location += 1
            continue

        cards.append({
            "RFI ID":            rfi_id,
            "Lead Title":        title,
            "Category":          category,
            "Location":          location,
            "Max Budget":        budget,
            "Respond By":        respond_by,
            "Contract Length":   contract_length,
            "Decision Deadline": decision_deadline,
        })

    if skipped_location:
        print(f"  🌍 Skipped {skipped_location} non-Singapore cards")

    return cards

# ---------------- ARIBA MAIN FLOW ---------------- #

def search_ariba(driver, keyword_string):
    """
    Full Ariba Discovery scrape:

      1. Navigate with ?serviceLocations=Singapore
         Ariba filters server-side — only SG leads are returned.

      2. Type the full keyword string into the search box.
         Ariba OR-matches every word, maximising recall.

      3. Set page size to 50 (confirmed Ariba maximum).

      4. Paginate using UI5 firePress() with extended button ID range (→500).

      5. parse_ariba_cards() applies a client-side Singapore check.
    """

    # Step 1 — load with Singapore server-side filter
    base_url = (
        "https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
        f"comsapsbncdiscoveryui#/leads/search"
        f"?serviceLocations={quote('Singapore', safe='')}"
    )
    print(f"\n🔍 Navigating to Ariba Discovery (Singapore filter)...")
    driver.get(base_url)
    time.sleep(5)

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "[class*='sapMListItem'], [class*='sapMLIB']"
            ))
        )
        print("  ✅ Lead list detected")
    except Exception:
        print("  ⚠️  Lead list not detected — proceeding anyway")

    time.sleep(2)
    check_ui5_available(driver)

    ids_sg_only = get_all_rfi_ids(driver)
    print(f"  📊 RFIs with SG filter only: {len(ids_sg_only)}")

    # Step 2 — type keywords into search box on top of SG filter
    print(f"\n  🔎 Submitting keyword search...")
    searched = type_into_search(driver, keyword_string)
    if not searched:
        print("  ⚠️  Keyword search failed — continuing with SG filter only")

    ids_after_search = get_all_rfi_ids(driver)
    print(f"  📊 RFIs after keyword search: {len(ids_after_search)}")

    # Step 3 — increase page size (max 50 on Ariba Discovery)
    print("\n  ⚙️  Setting page size...")
    set_page_size(driver)
    time.sleep(2)

    ids_after_resize = get_all_rfi_ids(driver)
    print(f"  📊 RFIs after page size change: {len(ids_after_resize)}")

    # Steps 4 & 5 — paginate and collect
    all_cards = []
    seen_ids  = set()
    page_num  = 1
    consecutive_empty = 0
    MAX_PAGES = 100

    while page_num <= MAX_PAGES:
        print(f"\n  📄 Scraping page {page_num}...")
        time.sleep(2)

        cards = parse_ariba_cards(driver)
        print(f"  Parsed {len(cards)} Singapore cards")

        if not cards:
            consecutive_empty += 1
            print(f"  ⚠️  Empty page ({consecutive_empty} consecutive)")
            if consecutive_empty >= 2:
                print("  ⏹  Two consecutive empty pages — done")
                break
        else:
            consecutive_empty = 0
            for card in cards:
                rid = card["RFI ID"]
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    all_cards.append(card)

        print(f"  Total unique cards so far: {len(all_cards)}")

        # Page counter check
        cur, total = get_page_numbers(driver)
        if cur and total:
            print(f"  📊 Page {cur} of {total}")
            if cur >= total:
                print("  ⏹  Last page reached (page counter)")
                break

        # Next button state check
        if is_next_disabled(driver):
            print("  ⏹  Next button disabled — last page")
            break

        # Snapshot state before clicking Next
        ids_before = get_all_rfi_ids(driver)
        fp_before  = get_content_fingerprint(driver)

        # Click Next
        clicked = click_next(driver)
        if not clicked:
            print("  ⏹  Could not click Next — stopping")
            break

        # Wait for new content — FIX: timeout now 60s (was 30s)
        changed = wait_for_next_page(driver, ids_before, fp_before, timeout=60)
        if not changed:
            print("  ⏹  Page did not change — stopping")
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

        cur_url = driver.current_url
        print(f"Post-login URL:   {cur_url}")
        print(f"Post-login title: {driver.title}")

        if "login" in cur_url.lower() or "authenticat" in cur_url.lower():
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
    Each lead's title + category is embedded and compared against every
    keyword embedding. Leads scoring >= threshold are kept.
    """
    out = []
    for lead in leads:
        title    = lead.get("Lead Title", "")
        category = lead.get("Category", "")

        if not index:
            lead["Matched_Keyword"] = "fallback"
            lead["Match_Score"]     = 1.0
            lead["Match_Category"]  = "General"
            out.append(lead)
            print(f"  ✅ No index — keeping: {title[:60]}")
            continue

        lead, score = enrich_lead_ai(lead, index)
        print(f"  SCORE {score:.3f} | {title[:60]} | {category[:40]}")

        if score >= threshold:
            out.append(lead)

    return out

# ---------------- MAIN ---------------- #

def main():
    ss = connect()

    # Scrape ALPS procurement pages
    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)
        write(get_ws(ss, name), data)
        print(f"✅ Written {len(data)} rows → '{name}'")

    # Load keywords from Google Sheet
    keywords = get_keywords(ss)
    if not keywords:
        print("❌ No keywords — check KEYWORDS sheet has a 'Keywords' column with data")
        return

    # Join ALL keywords into one string.
    # Ariba OR-matches every word so using all keywords maximises recall.
    keyword_string = " ".join(keywords)
    print(f"\n🔑 Search string ({len(keywords)} keywords): {keyword_string[:120]}...")

    # Build semantic index for AI filtering
    index = build_keyword_index(keywords)
    print(f"✅ Keyword index built: {len(index)} entries")

    # Scrape Ariba with Singapore filter + keyword search
    raw = run_ariba(keyword_string)
    print(f"\nRAW RESULTS: {len(raw)}")

    if not raw:
        print("❌ No results from Ariba")
        return

    # Deduplicate by RFI ID
    seen    = set()
    deduped = []
    for r in raw:
        rid = r.get("RFI ID", "")
        if rid and rid not in seen:
            seen.add(rid)
            deduped.append(r)
        elif not rid:
            deduped.append(r)
    print(f"AFTER DEDUP: {len(deduped)}")

    # AI relevance filter
    final = ai_filter(deduped, index)
    print(f"FINAL (after AI filter): {len(final)}")

    # Write to Google Sheet
    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")


if __name__ == "__main__":
    main()

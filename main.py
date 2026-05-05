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

# ---------------- SCRAPER (ALPS) ---------------- #

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

# ---------------- DRIVER ---------------- #

def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # Use a standard 1280x900 window — keeps pagination bar inside clickable area
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

# ---------------- UI5 HELPERS ---------------- #

def check_ui5_available(driver):
    """Confirm sap.ui is loaded and log how many controls are registered."""
    result = driver.execute_script("""
        try {
            var v = sap.ui.version;
            var els = sap.ui.getCore().mElements || sap.ui.getCore()._mElements || {};
            return 'UI5 v' + v + ', ' + Object.keys(els).length + ' controls';
        } catch(e) {
            return 'unavailable: ' + e.message;
        }
    """)
    print(f"  🔧 UI5: {result}")
    return "unavailable" not in result


def set_page_size(driver):
    """
    Open the UI5 sapMSlt items-per-page dropdown and select the largest option.
    UI5 renders the option list as a page-level popup — we click the control,
    wait for the popup, then click the biggest option we can find.
    """
    try:
        # Find the custom select control
        controls = driver.find_elements(By.CSS_SELECTOR, "[class*='sapMSlt']")
        if not controls:
            print("  ⚠️  No sapMSlt found — skipping page size change")
            return

        ctrl = controls[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ctrl)
        time.sleep(0.5)

        # Open the popup via UI5 API if possible, else DOM click
        opened = driver.execute_script("""
            try {
                var c = sap.ui.getCore().byId(arguments[0].id);
                if (c && typeof c.open === 'function') { c.open(); return 'api'; }
                if (c && typeof c.fireChange === 'function') {
                    arguments[0].click(); return 'dom-click';
                }
            } catch(e) {}
            arguments[0].click();
            return 'dom-click';
        """, ctrl)
        print(f"  📋 Opened page-size dropdown via: {opened}")
        time.sleep(1.5)  # wait for UI5 popup animation

        # The popup list renders at document root — find all visible option items
        for val in ["100", "50", "25"]:
            opts = driver.find_elements(By.XPATH,
                f"//*[@role='option' and contains(normalize-space(.), '{val}')]"
            )
            if not opts:
                opts = driver.find_elements(By.XPATH,
                    f"//*[contains(@class,'sapMSelectListItem') and normalize-space(text())='{val}']"
                )
            if opts:
                driver.execute_script("arguments[0].click();", opts[0])
                print(f"  ✅ Set page size to {val}")
                time.sleep(2)
                return

        # Could not find option — close popup and continue
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        print("  ⚠️  Could not find size option — using default")

    except Exception as e:
        print(f"  ⚠️  set_page_size error: {e}")


# ---------------- PAGINATION ---------------- #

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
    Try to extract (current_page, total_pages) from the UI5 pagination control.
    Ariba renders this as plain text like '1 of 12' or '1/12'.
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
    """Short fingerprint of visible content for change detection."""
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'RFI\s*[\u00b7\u2022\-|]\s*\d{7,12}', text, re.IGNORECASE)
        return text[m.start(): m.start() + 400] if m else text[:400]
    except Exception:
        return ""


def is_next_button_disabled(driver):
    """Return True if the Next Page button exists but is aria-disabled."""
    try:
        btns = driver.find_elements(By.CSS_SELECTOR,
            "button[aria-label*='Next Page'], button[aria-label*='Next']")
        for btn in btns:
            aria = btn.get_attribute("aria-disabled")
            if aria in ("true", "True", True):
                return True
        return False
    except Exception:
        return False


def click_next(driver):
    """
    Click the Next Page button using UI5-native firePress first,
    then fall back to DOM-level strategies.

    Root cause of the "click registers but page never changes" issue:
    ActionChains computes click coordinates from getBoundingClientRect().
    When the button is at left:1291 in a 1920px window, those coordinates
    land outside the headless Chrome input region. We fix this by:
      1. Using UI5's own firePress() — no coordinates needed
      2. Falling back to JS .click() directly on the element
      3. Finally trying ActionChains on a smaller viewport
    """

    # ── Strategy 1: UI5 firePress via sap.ui.getCore() control scan ──
    result = driver.execute_script("""
        try {
            var core = sap.ui.getCore();
            var els = core.mElements || core._mElements || {};

            // Walk all registered UI5 controls looking for Next button
            for (var id in els) {
                var el = els[id];
                if (!el) continue;
                try {
                    var dom = el.getDomRef ? el.getDomRef() : null;
                    if (!dom) continue;
                    var label = dom.getAttribute('aria-label') || '';
                    var title = dom.getAttribute('title') || '';
                    if ((label.indexOf('Next') !== -1 || title.indexOf('Next') !== -1)
                            && dom.tagName === 'BUTTON') {
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

    if result:
        print(f"  ➡️  [S1] UI5 firePress — {result}")
        return True

    # ── Strategy 2: firePress by known button ID patterns ──
    result = driver.execute_script("""
        try {
            var core = sap.ui.getCore();
            // Try button IDs Ariba typically uses for pagination
            var candidates = ['__button37','__button38','__button39','__button36'];
            for (var i = 0; i < candidates.length; i++) {
                var ctrl = core.byId(candidates[i]);
                if (!ctrl) continue;
                var dom = ctrl.getDomRef ? ctrl.getDomRef() : null;
                if (!dom) continue;
                var label = dom.getAttribute('aria-label') || '';
                if (label.indexOf('Next') !== -1 && typeof ctrl.firePress === 'function') {
                    ctrl.firePress();
                    return 'firePress-id:' + candidates[i];
                }
            }
        } catch(e) {}
        return null;
    """)

    if result:
        print(f"  ➡️  [S2] UI5 firePress by ID — {result}")
        return True

    # ── Strategy 3: Click the last button inside the pagination wrapper ──
    result = driver.execute_script("""
        try {
            // Ariba wraps pagination in .discovery-pagination-wrapper
            var wrapper = document.querySelector(
                '.discovery-pagination-wrapper, [class*="pagination"]'
            );
            if (wrapper) {
                var btns = wrapper.querySelectorAll('button');
                // Last button is always Next
                var last = btns[btns.length - 1];
                if (last && !last.disabled
                        && last.getAttribute('aria-disabled') !== 'true') {
                    last.click();
                    return 'dom-last-in-wrapper';
                }
            }
        } catch(e) {}
        return null;
    """)

    if result:
        print(f"  ➡️  [S3] DOM last-button-in-wrapper — {result}")
        return True

    # ── Strategy 4: JS dispatchEvent on the button element directly ──
    dispatched = driver.execute_script("""
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

    if dispatched:
        print(f"  ➡️  [S4] dispatchEvent — {dispatched}")
        return True

    # ── Strategy 5: ActionChains after scrolling button into center ──
    try:
        btn = None
        for sel in ["button[aria-label*='Next Page']", "button[aria-label*='Next']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    aria = el.get_attribute("aria-disabled")
                    if aria not in ("true", "True"):
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

    print("  ❌ All Next click strategies exhausted — on last page")
    return False


def wait_for_next_page(driver, old_ids, old_fingerprint, timeout=30):
    """
    Wait until the page content actually changes after clicking Next.
    Polls two signals: RFI ID set and content fingerprint.
    Returns True if change detected, False if timeout or last page.
    """
    # Brief pause to let UI5 start its XHR / re-render cycle
    time.sleep(2)

    deadline = time.time() + timeout
    poll = 0

    while time.time() < deadline:
        time.sleep(1)
        poll += 1

        try:
            new_ids = get_all_rfi_ids(driver)
            new_fp = get_content_fingerprint(driver)

            # Content changed — new page is ready
            if new_ids and new_ids != old_ids:
                print(f"  ✅ Page changed (RFI set) after {poll}s")
                return True

            if new_fp and new_fp != old_fingerprint:
                print(f"  ✅ Page changed (content fingerprint) after {poll}s")
                return True

            # Next button became disabled — we just loaded the last page's content
            if is_next_button_disabled(driver):
                print("  ⏹  Next button disabled — last page content loaded")
                return True  # return True so we still scrape this page's cards

            if poll % 5 == 0:
                print(f"    [poll {poll}s] waiting... RFIs on page: {len(new_ids)}")

        except Exception as e:
            print(f"    [poll {poll}s] error: {e}")

    print(f"  ⚠️  No change after {timeout}s — stopping pagination")
    return False


# ---------------- CARD PARSING ---------------- #

def is_singapore(location_str):
    if not location_str:
        return True  # blank location = keep (many SG tenders leave this empty)
    return any(term in location_str.lower() for term in ALLOWED_LOCATIONS)


def parse_ariba_cards(driver):
    """
    Parse all lead cards from the current page's rendered text.
    Filters to Singapore leads only.
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
    print(f"  Found {len(matches)} RFI markers")

    skipped_location = 0

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

        start = max(0, match.start() - 300)
        end = (matches[idx + 1].start()
               if idx + 1 < len(matches)
               else match.end() + 500)
        block = full_text[start:end].strip()
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        title = category = location = budget = ""
        respond_by = contract_length = decision_deadline = ""

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
            print(f"  🌍 Skipping non-SG: '{title[:50]}' (location: {location})")
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
        print(f"  🌍 Skipped {skipped_location} non-Singapore cards")

    return cards


# ---------------- ARIBA MAIN FLOW ---------------- #

def search_ariba(driver):
    """
    Navigate to Ariba Discovery leads, set page size, then paginate
    through every page collecting Singapore leads.

    Key decisions:
    - No serviceLocations filter in the URL: Ariba applies it server-side
      and excludes leads with blank location — which is most SG leads.
      We filter client-side via is_singapore() instead.
    - Use UI5 firePress() for Next clicks: avoids coordinate-based click
      failures caused by the button rendering at x>1280 in wide viewports.
    """
    base_url = (
        "https://portal.us.bn.cloud.ariba.com/dashboard/appext/"
        "comsapsbncdiscoveryui#/leads/search"
    )

    print(f"\n🔍 Navigating to Ariba Discovery...")
    driver.get(base_url)
    time.sleep(5)

    # Wait for the lead list to render
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

    # Confirm UI5 framework is available
    check_ui5_available(driver)

    initial_ids = get_all_rfi_ids(driver)
    print(f"  📊 Initial RFIs visible: {len(initial_ids)}")

    # Increase items per page before starting
    print("\n  ⚙️  Setting page size...")
    set_page_size(driver)
    time.sleep(2)

    after_resize_ids = get_all_rfi_ids(driver)
    print(f"  📊 RFIs after page size change: {len(after_resize_ids)}")

    all_cards = []
    seen_ids = set()
    page_num = 1
    consecutive_empty = 0
    MAX_PAGES = 100  # safety cap

    while page_num <= MAX_PAGES:
        print(f"\n  📄 Scraping page {page_num}...")
        time.sleep(2)

        cards = parse_ariba_cards(driver)
        print(f"  Parsed {len(cards)} Singapore cards on page {page_num}")

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

        # Check if we know we're on the last page
        cur, total = get_page_numbers(driver)
        if cur and total:
            print(f"  📊 Page {cur} of {total}")
            if cur >= total:
                print("  ⏹  Last page reached (page counter)")
                break

        if is_next_button_disabled(driver):
            print("  ⏹  Next button is disabled — last page")
            break

        # Snapshot state before clicking Next
        ids_before = get_all_rfi_ids(driver)
        fp_before = get_content_fingerprint(driver)

        # Click Next
        clicked = click_next(driver)
        if not clicked:
            print("  ⏹  Could not click Next — stopping")
            break

        # Wait for new page content to load
        changed = wait_for_next_page(driver, ids_before, fp_before, timeout=30)
        if not changed:
            # Timeout — scrape whatever is on screen then stop
            print("  ⏹  Page did not change — stopping")
            break

        page_num += 1

    print(f"\n✅ Total Singapore cards scraped: {len(all_cards)}")
    return all_cards


def run_ariba():
    driver = build_driver()
    try:
        driver.get("about:blank")
        print("✅ Driver OK")

        login(driver)

        cur_url = driver.current_url
        print(f"Post-login URL: {cur_url}")
        print(f"Post-login title: {driver.title}")

        if "login" in cur_url.lower() or "authenticat" in cur_url.lower():
            print("❌ Login may have failed")
            return []

        return search_ariba(driver)

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
    Keep leads whose title+category semantically matches the keyword index
    above the threshold. Threshold 0.35 is calibrated to catch logistics/
    supply chain leads (~0.39) while dropping unrelated ones.
    """
    out = []
    for lead in leads:
        title = lead.get("Lead Title", "")
        category = lead.get("Category", "")

        if not index:
            lead["AI_Matched_Keyword"] = "fallback"
            lead["AI_Match_Score"] = 1.0
            lead["AI_Category"] = "General"
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

    # Load keywords from sheet
    keywords = get_keywords(ss)
    if not keywords:
        print("❌ No keywords — check KEYWORDS sheet has a 'Keywords' column")
        return

    # Build semantic index
    index = build_keyword_index(keywords)
    print(f"✅ Keyword index: {len(index)} entries")

    # Scrape Ariba (no keyword string needed — we filter AI-side)
    raw = run_ariba()

    print(f"\nRAW RESULTS: {len(raw)}")

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
        print("❌ No results from Ariba")
        return

    # AI filter
    final = ai_filter(deduped, index)
    print(f"FINAL (after AI filter): {len(final)}")

    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")


if __name__ == "__main__":
    main()

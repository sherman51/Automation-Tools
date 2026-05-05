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


def score_lead(lead, keyword_index, keywords):
    """
    Multi-signal relevance scorer.

    Signal 1 — Exact/partial keyword match (weight 0.50)
        Counts how many keywords appear as substrings in the lead text.
        Normalised against 15% of total keywords so a lead must hit
        multiple keywords to score well here.
        Example: 25 keywords → need ~4 hits to max this signal.

    Signal 2 — Top-3 semantic similarity average (weight 0.40)
        Averages the three highest cosine similarities instead of just
        the best. A lead must be semantically close to MULTIPLE keywords
        to score well — a single accidental high match gets diluted.

    Signal 3 — Category field boost (weight 0.10)
        If the Ariba category label contains a high-value term, add a
        small additive boost so clearly relevant leads with sparse titles
        are not accidentally dropped.

    Final score = weighted sum capped at 1.0.
    Recommended threshold: 0.5

    """
    title    = lead.get("Lead Title", "").lower()
    category = lead.get("Category", "").lower()
    text     = f"{title} {category}"

    if not text.strip():
        return 0.0, [], "none"

    # ── Signal 1: Exact keyword hit count ──────────────────────────────
    hits = []
    for kw in keywords:
        if kw.lower() in text:
            hits.append(kw)

    # Normalise: hitting 15%+ of all keywords maxes out this signal
    exact_score = min(len(hits) / max(len(keywords) * 0.15, 1), 1.0)

    # ── Signal 2: Top-3 semantic similarity average ────────────────────
    if keyword_index:
        text_vec = embed(text)
        sims = sorted(
            [cosine_similarity(text_vec, vec) for vec in keyword_index.values()],
            reverse=True
        )
        top_n = sims[:3]
        semantic_score = sum(top_n) / len(top_n)
    else:
        semantic_score = 0.0

    # ── Signal 3: Category field boost ────────────────────────────────
    boost_terms = [
        "pharma", "drug", "medicine", "vaccine", "clinical",
        "logistics", "supply chain", "cold chain", "distribution",
        "warehouse", "hospital", "medical"
    ]
    category_boost = 0.10 if any(t in category for t in boost_terms) else 0.0

    # ── Weighted final score ───────────────────────────────────────────
    final = (exact_score * 0.50) + (semantic_score * 0.40) + category_boost
    final = round(min(final, 1.0), 3)

    match_cat = _classify(hits, title, category)
    return final, hits, match_cat


def _classify(hits, title, category):
    text = f"{title} {category}"
    if any(x in text for x in ["drug", "pharma", "vaccine", "clinical", "medical", "hospital"]):
        return "Pharma/Medical"
    elif any(x in text for x in ["logistics", "supply chain", "warehouse", "distribution", "cold chain"]):
        return "Logistics"
    elif any(x in text for x in ["it", "software", "cloud", "system", "digital"]):
        return "IT"
    return "General"


def enrich_lead_ai(lead, keyword_index, keywords):
    score, hits, match_cat = score_lead(lead, keyword_index, keywords)
    lead["Match_Score"]       = score
    lead["Matched_Keywords"]  = ", ".join(hits) if hits else ""
    lead["Keyword_Hit_Count"] = len(hits)
    lead["Match_Category"]    = match_cat
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
ARIBA_BASE_LINK = "https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/RfxEvent/preview/"

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
    # At wider viewports the Next button renders off-screen and clicks
    # are silently ignored by headless Chrome.
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
    Open the UI5 sapMSlt items-per-page dropdown and select the largest
    available option. Ariba Discovery max is 50 — do NOT try 100 as it
    does not exist and clicking it may select the wrong element.
    """
    try:
        controls = driver.find_elements(By.CSS_SELECTOR, "[class*='sapMSlt']")
        if not controls:
            print("  ⚠️  No page-size control found — using default")
            return

        ctrl = controls[0]
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ctrl)
        time.sleep(0.5)

        driver.execute_script("""
            try {
                var c = sap.ui.getCore().byId(arguments[0].id);
                if (c && typeof c.open === 'function') { c.open(); return; }
            } catch(e) {}
            arguments[0].click();
        """, ctrl)
        time.sleep(1.5)

        # FIX: Removed "100" — confirmed Ariba Discovery max is 50
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

        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        print("  ⚠️  Page size option not found — using default")

    except Exception as e:
        print(f"  ⚠️  set_page_size error: {e}")


# ---------------- SEARCH BOX ---------------- #

def type_into_search(driver, keyword_string):
    """
    Type the full keyword string into Ariba's search box.
    Ariba OR-matches every word so using all keywords maximises recall —
    any lead containing any keyword will appear in results.
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
    Print all visible buttons on the page to diagnose pagination failures.
    Called automatically when all Next click strategies are exhausted.
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

    Why firePress() and not ActionChains:
    ActionChains computes coordinates from getBoundingClientRect(). At wide
    viewports the button renders off-screen and the click is silently
    ignored even though Selenium reports success. firePress() goes through
    UI5's own event bus with no coordinates needed.

    FIX: Extended button ID scan range from 60 → 500 to handle deep
    pagination (IDs increment ~9 per page; page 42 needs ~__button420).
    FIX: Strategy 3 now attempts UI5 firePress() via the element's own ID
    before falling back to plain .click(), which UI5 often ignores.
    FIX: debug_buttons() called when all strategies exhausted.
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
    # FIX: Extended upper bound 60 → 500
    # Pattern: IDs increment ~9 per page → page 42 needs ~__button420
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
    # FIX: Try UI5 firePress() via element ID before plain DOM .click()
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

    # All strategies exhausted — dump buttons for diagnosis
    print("  ❌ All Next click strategies exhausted")
    debug_buttons(driver)
    return False


# ---------------- WAIT FOR PAGE CHANGE ---------------- #

def wait_for_next_page(driver, old_ids, old_fingerprint, timeout=60):
    """
    Poll until the page content changes after clicking Next.
    FIX: Default timeout increased 30s → 60s for slow Ariba XHR responses.

    Two independent change signals:
      A) RFI ID frozenset differs from the previous page
      B) Content fingerprint (first 400 chars around first card) differs
    Also exits early if Next becomes disabled (just loaded last page).
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
    Blank kept because many SG tenders don't populate the location field.
    """
    if not location_str:
        return True
    return any(term in location_str.lower() for term in ALLOWED_LOCATIONS)


def parse_ariba_cards(driver):
    """
    Parse all lead cards from the current page's rendered innerText.

    FIX: Block start boundary changed from fixed -300 chars to
    matches[idx-1].end() so each card's text window is cleanly isolated
    from the previous card. Prevents previous card's footer lines
    (category, budget, deadline) from bleeding into this card's title.

    FIX: Title selection now walks backwards from the RFI line skipping
    any line that starts with a known field prefix, so field labels from
    a prior card can never be mistaken for the title.
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
    print(f"  Found {len(matches)} RFI markers on page")

    skipped_location = 0

    # Field prefixes to skip when walking backwards for the title
    field_prefixes = (
        "category:", "service locations:", "max budget:",
        "respond by:", "contract length:", "decision deadline:",
        "rfi", "rfp", "rfq"
    )

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

        # FIX: Start block right after the previous RFI marker ends,
        # not 300 chars before the current one
        if idx > 0:
            start = matches[idx - 1].end()
        else:
            start = max(0, match.start() - 400)

        end = (matches[idx + 1].start()
               if idx + 1 < len(matches)
               else match.end() + 600)

        block = full_text[start:end].strip()
        lines = [l.strip() for l in block.split("\n") if l.strip()]

        title = category = location = budget = ""
        respond_by = contract_length = decision_deadline = ""

        # FIX: Walk backwards from the RFI line, skip field label lines
        for i, line in enumerate(lines):
            if rfi_pattern.search(line):
                for j in range(i - 1, -1, -1):
                    candidate = lines[j].strip()
                    if not candidate:
                        continue
                    if candidate.lower().startswith(field_prefixes):
                        continue
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
            "Link":              f'=HYPERLINK("{link}", "View")',
        })

    if skipped_location:
        print(f"  🌍 Skipped {skipped_location} non-Singapore cards")

    return cards


# ---------------- ARIBA MAIN FLOW ---------------- #

def search_ariba(driver, keyword_string):
    """
    Full Ariba Discovery scrape:
      1. Navigate with ?serviceLocations=Singapore (server-side filter)
      2. Type keyword string into search box (OR-match maximises recall)
      3. Set page size to 50 (confirmed Ariba Discovery maximum)
      4. Paginate via UI5 firePress() with extended ID range (→500)
      5. parse_ariba_cards() isolates card blocks and extracts clean titles
    """
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

    print(f"\n  🔎 Submitting keyword search...")
    searched = type_into_search(driver, keyword_string)
    if not searched:
        print("  ⚠️  Keyword search failed — continuing with SG filter only")

    ids_after_search = get_all_rfi_ids(driver)
    print(f"  📊 RFIs after keyword search: {len(ids_after_search)}")

    print("\n  ⚙️  Setting page size...")
    set_page_size(driver)
    time.sleep(2)

    ids_after_resize = get_all_rfi_ids(driver)
    print(f"  📊 RFIs after page size change: {len(ids_after_resize)}")

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

        cur, total = get_page_numbers(driver)
        if cur and total:
            print(f"  📊 Page {cur} of {total}")
            if cur >= total:
                print("  ⏹  Last page reached (page counter)")
                break

        if is_next_disabled(driver):
            print("  ⏹  Next button disabled — last page")
            break

        ids_before = get_all_rfi_ids(driver)
        fp_before  = get_content_fingerprint(driver)

        clicked = click_next(driver)
        if not clicked:
            print("  ⏹  Could not click Next — stopping")
            break

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

def ai_filter(leads, index, keywords, threshold=0.5):
    """
    Multi-signal relevance filter.

    Threshold raised from 0.35 → 0.5 to match the new scorer which
    combines three signals:
      - Exact keyword hit count   (weight 0.50) — must hit multiple keywords
      - Top-3 semantic similarity (weight 0.40) — must be broadly relevant
      - Category field boost      (weight 0.10) — rewards pharma/logistics labels

    Prints per-lead detail for easy auditing. Matched_Keywords and
    Keyword_Hit_Count are written to the sheet so you can review why
    each lead was kept and tune the threshold if needed.
    """
    out = []
    for lead in leads:
        if not index:
            lead["Matched_Keywords"]   = "fallback"
            lead["Match_Score"]        = 1.0
            lead["Keyword_Hit_Count"]  = 0
            lead["Match_Category"]     = "General"
            out.append(lead)
            continue

        lead, score = enrich_lead_ai(lead, index, keywords)
        status = "✅ KEEP" if score >= threshold else "❌ DROP"
        print(
            f"  {status} {score:.3f} | hits={lead['Keyword_Hit_Count']:2d} "
            f"| {lead.get('Lead Title','')[:55]} "
            f"| [{lead.get('Matched_Keywords','')[:60]}]"
        )

        if score >= threshold:
            out.append(lead)

    kept    = len(out)
    dropped = len(leads) - kept
    print(f"\n  📊 Filter result: {kept} kept, {dropped} dropped (threshold={threshold})")
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

    # Join ALL keywords into one string for Ariba search box.
    # Ariba OR-matches every word so using all keywords maximises recall.
    keyword_string = " ".join(keywords)
    print(f"\n🔑 Search string ({len(keywords)} keywords): {keyword_string[:120]}...")

    # Build semantic index for AI filtering
    index = build_keyword_index(keywords)
    print(f"✅ Keyword index built: {len(index)} entries")

    # Scrape Ariba
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

    # AI relevance filter — pass full keywords list for exact-match signal
    final = ai_filter(deduped, index, keywords)
    print(f"FINAL (after AI filter): {len(final)}")

    # Write to Google Sheet
    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")


if __name__ == "__main__":
    main()

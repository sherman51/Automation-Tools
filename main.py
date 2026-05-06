import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import re
import time
import numpy as np
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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


def build_keyword_index(keyword_weights):
    """
    Build a semantic index from a weighted keyword dict.

    Args:
        keyword_weights: dict of {keyword (str): weight (float)}

    Returns:
        dict of {keyword: {"vec": np.array, "weight": float}}
    """
    index = {}
    for kw, weight in keyword_weights.items():
        index[kw] = {
            "vec": embed(kw),
            "weight": weight,
        }
    return index


def score_lead(lead, keyword_index, keyword_weights):
    """
    Multi-signal relevance scorer with weighted keywords.

    Signal 1 — Weighted exact/partial keyword match (weight 0.50)
        Sums the row-weights of every keyword that appears as a substring
        in the lead text. Normalised against the maximum possible weighted
        sum so that hitting a high-weight group (e.g. PHI names at 0.7)
        contributes far more than hitting low-weight terms.

        Example: hitting "TTSH" (group weight 0.7) scores much higher than
        hitting "cold chain" (group weight 0.1).

    Signal 2 — Top-3 weighted semantic similarity average (weight 0.40)
        Computes cosine similarity for all keywords, multiplies each by its
        group weight, then averages the top-3 weighted scores. This means
        semantic closeness to a high-weight PHI name matters more than
        closeness to a generic logistics term.

    Signal 3 — Category field boost (weight 0.10)
        Small additive boost when the Ariba category label contains a
        high-value term.

    Final score = weighted sum capped at 1.0.
    Recommended threshold: 0.5
    """
    title    = lead.get("Lead Title", "").lower()
    category = lead.get("Category", "").lower()
    text     = f"{title} {category}"

    if not text.strip():
        return 0.0, [], "none"

    # ── Signal 1: Weighted exact keyword hit sum ───────────────────────
    hits = []
    weighted_hit_sum = 0.0
    max_possible_weight = sum(keyword_weights.values()) if keyword_weights else 1.0

    for kw, weight in keyword_weights.items():
        if kw.lower() in text:
            hits.append(kw)
            weighted_hit_sum += weight

    # Normalise against max possible weight sum
    exact_score = min(weighted_hit_sum / max(max_possible_weight * 0.15, 0.01), 1.0)

    # ── Signal 2: Top-3 weighted semantic similarity average ───────────
    if keyword_index:
        text_vec = embed(text)
        weighted_sims = sorted(
            [
                cosine_similarity(text_vec, entry["vec"]) * entry["weight"]
                for entry in keyword_index.values()
            ],
            reverse=True
        )
        top_n = weighted_sims[:3]
        # Normalise: max possible per entry is 1.0 * max_weight
        max_weight = max(keyword_weights.values()) if keyword_weights else 1.0
        semantic_score = (sum(top_n) / len(top_n)) / max_weight if top_n else 0.0
        semantic_score = min(semantic_score, 1.0)
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


def enrich_lead_ai(lead, keyword_index, keyword_weights):
    score, hits, match_cat = score_lead(lead, keyword_index, keyword_weights)
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

# ---------------- EMAIL CONFIG ---------------- #

SMTP_HOST      = "smtp.office365.com"
SMTP_PORT      = 587
SMTP_USER      = os.getenv("SMTP_USER", "")       # your Outlook email
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")   # your Outlook app password
EMAIL_LIST_SHEET   = "Email List"
ALERTED_IDS_SHEET  = "Alerted IDs"
EMAIL_ALERT_THRESHOLD = 0.7

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


# ---------------- KEYWORDS (WEIGHTED) ---------------- #

def _parse_date(date_str):
    """Parse a date string like '17 Apr 2026' into a date object. Returns None if blank/invalid."""
    if not date_str or not date_str.strip():
        return None
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _is_active(row):
    """
    Return True if this keyword row should be used today.
    - Commission Date must be today or in the past (or blank → always active)
    - Decommission Date must be in the future (or blank → never expires)
    """
    today = datetime.today().date()

    commission_str   = row.get("Commission Date", "").strip()
    decommission_str = row.get("Decommission Date", "").strip()

    commission_date   = _parse_date(commission_str)
    decommission_date = _parse_date(decommission_str)

    if commission_date and commission_date > today:
        return False  # not yet active

    if decommission_date and decommission_date <= today:
        return False  # expired

    return True


def get_keywords(ss):
    """
    Load keywords from the KEYWORDS sheet and return a weighted dict.

    Expected columns: S/N | Keywords | Commission Date | Decommission Date | Weighted % | Remarks

    Returns:
        dict of {keyword (str): weight (float)}

    Behaviour:
    - Rows whose Commission Date is in the future are skipped.
    - Rows whose Decommission Date is today or past are skipped.
    - The 'Weighted %' column is parsed as a float (e.g. "0.7" → 0.7).
      If missing or unparseable, defaults to 1.0 so existing rows still work.
    - Each row's keyword string is split on whitespace, commas, or semicolons.
      Every individual token inherits the row's weight.
    - Duplicate tokens keep the HIGHEST weight seen across rows.
    """
    try:
        ws  = ss.worksheet("KEYWORDS")
        rows = ws.get_all_records()

        keyword_weights = {}
        skipped_inactive = 0
        skipped_no_kw    = 0

        for row in rows:
            # ── Date-based activation check ───────────────────────────
            if not _is_active(row):
                skipped_inactive += 1
                continue

            # ── Parse weight ──────────────────────────────────────────
            raw_weight = str(row.get("Weighted %", "") or "").strip()
            try:
                weight = float(raw_weight)
            except ValueError:
                weight = 1.0  # default if column is missing or non-numeric

            # ── Parse keywords ────────────────────────────────────────
            raw_kw = (row.get("Keywords") or "").strip()
            if not raw_kw:
                skipped_no_kw += 1
                continue

            # Split on whitespace, comma, or semicolon
            tokens = re.split(r'[,;\s]+', raw_kw)
            for token in tokens:
                token = token.strip().lower()
                if not token:
                    continue
                # Keep highest weight if token appears in multiple rows
                if token in keyword_weights:
                    keyword_weights[token] = max(keyword_weights[token], weight)
                else:
                    keyword_weights[token] = weight

        print(
            f"✅ Loaded {len(keyword_weights)} weighted keywords "
            f"({skipped_inactive} rows skipped — inactive, "
            f"{skipped_no_kw} rows skipped — no keywords)"
        )

        # Debug: show a sample of keywords with their weights
        sample = list(keyword_weights.items())[:12]
        for kw, w in sample:
            print(f"   {w:.2f}  {kw}")
        if len(keyword_weights) > 12:
            print(f"   ... and {len(keyword_weights) - 12} more")

        return keyword_weights

    except Exception as e:
        print(f"⚠️ Could not load KEYWORDS sheet: {e}")
        return {}


# ---------------- SELENIUM DRIVER ---------------- #

def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
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
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        pattern = re.compile(r'RFI\s*[\u00b7\u2022\-|]\s*(\d{7,12})', re.IGNORECASE)
        return frozenset(pattern.findall(text))
    except Exception:
        return frozenset()


def get_page_numbers(driver):
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'(?:page\s+)?(\d+)\s*(?:of|/)\s*(\d+)', text, re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None, None


def get_content_fingerprint(driver):
    try:
        text = driver.execute_script("return document.body.innerText || '';")
        m = re.search(r'RFI\s*[\u00b7\u2022\-|]\s*\d{7,12}', text, re.IGNORECASE)
        return text[m.start(): m.start() + 400] if m else text[:400]
    except Exception:
        return ""


def is_next_disabled(driver):
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

    print("  ❌ All Next click strategies exhausted")
    debug_buttons(driver)
    return False


# ---------------- WAIT FOR PAGE CHANGE ---------------- #

def wait_for_next_page(driver, old_ids, old_fingerprint, timeout=60):
    time.sleep(2)

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

    field_prefixes = (
        "category:", "service locations:", "max budget:",
        "respond by:", "contract length:", "decision deadline:",
        "rfi", "rfp", "rfq"
    )

    for idx, match in enumerate(matches):
        rfi_id = match.group(2).strip()

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
            "Link":              f"https://portal.us.bn.cloud.ariba.com/dashboard/appext/comsapsbncdiscoveryui#/RfxEvent/preview/{rfi_id}",
        })

    if skipped_location:
        print(f"  🌍 Skipped {skipped_location} non-Singapore cards")

    return cards


# ---------------- ARIBA MAIN FLOW ---------------- #

def search_ariba(driver, keyword_string):
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

def ai_filter(leads, index, keyword_weights, threshold=0.5):
    """
    Multi-signal relevance filter using weighted keywords.

    threshold=0.5 is appropriate for the weighted scorer:
      - Hitting a single high-weight PHI name (0.7 group) alone scores ~0.35
        (Signal 1 exact hit * 0.50 weight + semantic contribution)
      - A lead must combine keyword hits + semantic similarity to clear 0.5
      - This prevents false positives from single-word PHI matches on
        unrelated tenders while still surfacing genuinely relevant leads

    Prints per-lead scores for auditing. The sheet includes:
      Match_Score, Matched_Keywords, Keyword_Hit_Count, Match_Category
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

        lead, score = enrich_lead_ai(lead, index, keyword_weights)
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


# ---------------- ALERTED IDS ---------------- #

def get_alerted_ids(ss):
    """
    Load all previously alerted RFI IDs from the 'Alerted IDs' sheet.
    Returns a set of RFI ID strings.
    Creates the sheet with headers if it doesn't exist yet.
    """
    try:
        ws = get_ws(ss, ALERTED_IDS_SHEET)
        records = ws.get_all_records()
        ids = {str(r.get("RFI ID", "")).strip() for r in records if r.get("RFI ID")}
        print(f"✅ Loaded {len(ids)} previously alerted RFI IDs")
        return ids
    except Exception as e:
        print(f"⚠️  Could not load Alerted IDs sheet: {e}")
        return set()


def save_alerted_ids(ss, new_leads):
    """
    Append newly alerted RFI IDs to the 'Alerted IDs' sheet.
    Each row records: RFI ID, Lead Title, Match Score, Date Alerted.
    """
    if not new_leads:
        return
    try:
        ws = get_ws(ss, ALERTED_IDS_SHEET)

        # Write headers if sheet is empty
        existing = ws.get_all_values()
        if not existing or not existing[0]:
            ws.append_row(["RFI ID", "Lead Title", "Match Score", "Date Alerted"])

        today = datetime.today().strftime("%d %b %Y %H:%M")
        rows = [
            [
                lead.get("RFI ID", ""),
                lead.get("Lead Title", ""),
                lead.get("Match_Score", ""),
                today,
            ]
            for lead in new_leads
        ]
        ws.append_rows(rows)
        print(f"✅ Saved {len(rows)} new RFI IDs to '{ALERTED_IDS_SHEET}'")
    except Exception as e:
        print(f"⚠️  Could not save Alerted IDs: {e}")


# ---------------- EMAIL LIST ---------------- #

def get_email_recipients(ss):
    """
    Load recipients from the 'Email List' sheet.
    Expected columns: S/N | Email | Name
    Returns a list of dicts: [{"email": ..., "name": ...}, ...]
    """
    try:
        ws      = ss.worksheet(EMAIL_LIST_SHEET)
        records = ws.get_all_records()
        recipients = []
        for r in records:
            email = (r.get("Email") or "").strip()
            name  = (r.get("Name")  or "").strip()
            if email and "@" in email:
                recipients.append({"email": email, "name": name or email})
        print(f"✅ Loaded {len(recipients)} email recipients")
        return recipients
    except Exception as e:
        print(f"⚠️  Could not load Email List sheet: {e}")
        return []


# ---------------- EMAIL SENDER ---------------- #

def _build_email_html(recipient_name, leads, run_date):
    """
    Build a clean HTML email body listing all new high-scoring leads.
    One email per recipient so we can personalise the greeting.
    """
    rows_html = ""
    for lead in leads:
        score_pct  = f"{lead.get('Match_Score', 0) * 100:.0f}%"
        title      = lead.get("Lead Title", "—")
        category   = lead.get("Category", "—")
        keywords   = lead.get("Matched_Keywords", "—")
        respond_by = lead.get("Respond By", "—") or "—"
        link       = lead.get("Link", "#")
        rfi_id     = lead.get("RFI ID", "—")

        rows_html += f"""
        <tr>
            <td style="padding:10px 8px;border-bottom:1px solid #eee;">
                <a href="{link}" style="font-weight:600;color:#0055a5;text-decoration:none;">
                    {title}
                </a><br>
                <span style="font-size:12px;color:#888;">RFI {rfi_id}</span>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #eee;text-align:center;">
                <span style="
                    background:#e6f4ea;color:#1a7340;
                    padding:3px 8px;border-radius:12px;
                    font-weight:700;font-size:13px;">
                    {score_pct}
                </span>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:13px;color:#555;">
                {category}
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:12px;color:#555;">
                {keywords}
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #eee;font-size:13px;color:#555;">
                {respond_by}
            </td>
        </tr>
        """

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;margin:0;padding:0;">
    <div style="max-width:900px;margin:30px auto;padding:0 20px;">

        <div style="background:#0055a5;padding:20px 24px;border-radius:8px 8px 0 0;">
            <h2 style="color:#fff;margin:0;font-size:20px;">🔔 Tender Alert — {run_date}</h2>
        </div>

        <div style="background:#f9f9f9;padding:20px 24px;border:1px solid #ddd;border-top:none;">
            <p style="margin:0 0 6px;">Hi <strong>{recipient_name}</strong>,</p>
            <p style="margin:0;">
                The following <strong>{len(leads)} new tender lead{"s" if len(leads) != 1 else ""}</strong>
                scored above {int(EMAIL_ALERT_THRESHOLD * 100)}% relevance in today's Ariba scan.
            </p>
        </div>

        <table style="width:100%;border-collapse:collapse;border:1px solid #ddd;border-top:none;">
            <thead>
                <tr style="background:#f0f4f9;">
                    <th style="padding:10px 8px;text-align:left;font-size:13px;">Lead Title</th>
                    <th style="padding:10px 8px;text-align:center;font-size:13px;">Score</th>
                    <th style="padding:10px 8px;text-align:left;font-size:13px;">Category</th>
                    <th style="padding:10px 8px;text-align:left;font-size:13px;">Matched Keywords</th>
                    <th style="padding:10px 8px;text-align:left;font-size:13px;">Respond By</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>

        <div style="padding:16px 0;font-size:12px;color:#aaa;text-align:center;">
            This is an automated alert. Do not reply to this email.
        </div>
    </div>
    </body></html>
    """
    return html


def send_alert_emails(recipients, new_leads, run_date):
    """
    Send one personalised HTML email per recipient listing all new leads.
    Uses Office365 SMTP with TLS (port 587).
    Skips sending if no recipients or no leads.
    """
    if not recipients:
        print("⚠️  No recipients found — skipping email")
        return
    if not new_leads:
        print("📭 No new leads above email threshold — skipping email")
        return
    if not SMTP_USER or not SMTP_PASSWORD:
        print("⚠️  SMTP_USER or SMTP_PASSWORD not set — skipping email")
        return

    subject = (
        f"[Tender Alert] {len(new_leads)} new lead"
        f"{'s' if len(new_leads) != 1 else ''} found — {run_date}"
    )

    sent  = 0
    failed = 0

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)

        for r in recipients:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = SMTP_USER
                msg["To"]      = r["email"]

                html_body = _build_email_html(r["name"], new_leads, run_date)
                msg.attach(MIMEText(html_body, "html"))

                server.sendmail(SMTP_USER, r["email"], msg.as_string())
                print(f"  ✉️  Sent to {r['name']} <{r['email']}>")
                sent += 1

            except Exception as e:
                print(f"  ❌ Failed to send to {r['email']}: {e}")
                failed += 1

        server.quit()

    except smtplib.SMTPAuthenticationError:
        print("❌ SMTP authentication failed — check SMTP_USER and SMTP_PASSWORD")
        return
    except Exception as e:
        print(f"❌ SMTP connection error: {e}")
        return

    print(f"\n✅ Email summary: {sent} sent, {failed} failed")


# ---------------- MAIN ---------------- #

def main():
    ss = connect()

    # Scrape ALPS procurement pages
    for url, name in URL_SHEET_MAP.items():
        html = fetch(url)
        data = extract_events(html, url)
        write(get_ws(ss, name), data)
        print(f"✅ Written {len(data)} rows → '{name}'")

    # Load weighted keywords from Google Sheet
    keyword_weights = get_keywords(ss)
    if not keyword_weights:
        print("❌ No keywords — check KEYWORDS sheet has 'Keywords' and 'Weighted %' columns")
        return

    # Join ALL keyword tokens into one string for Ariba search box.
    # Ariba OR-matches every word so using all keywords maximises recall.
    keyword_string = " ".join(keyword_weights.keys())
    print(f"\n🔑 Search string ({len(keyword_weights)} tokens): {keyword_string[:120]}...")

    # Build weighted semantic index for AI filtering
    index = build_keyword_index(keyword_weights)
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

    # AI relevance filter — sheet threshold 0.5
    final = ai_filter(deduped, index, keyword_weights, threshold=0.5)
    print(f"FINAL (after AI filter): {len(final)}")

    # Write all filtered leads (score >= 0.5) to Google Sheet
    write(get_ws(ss, TENDER_ALERTS_SHEET), final)
    print(f"✅ Written to '{TENDER_ALERTS_SHEET}' sheet")

    # ── Email alert: only NEW leads above 0.7 threshold ───────────────
    run_date     = datetime.today().strftime("%d %b %Y")
    alerted_ids  = get_alerted_ids(ss)
    recipients   = get_email_recipients(ss)

    # Filter to high-scoring leads not previously alerted
    high_score_leads = [
        lead for lead in final
        if lead.get("Match_Score", 0) >= EMAIL_ALERT_THRESHOLD
    ]
    new_leads = [
        lead for lead in high_score_leads
        if str(lead.get("RFI ID", "")).strip() not in alerted_ids
    ]

    print(f"\n📊 Email alert summary:")
    print(f"   Leads above {int(EMAIL_ALERT_THRESHOLD * 100)}% threshold : {len(high_score_leads)}")
    print(f"   Already alerted (skipped)    : {len(high_score_leads) - len(new_leads)}")
    print(f"   New leads to email           : {len(new_leads)}")

    if new_leads:
        send_alert_emails(recipients, new_leads, run_date)
        save_alerted_ids(ss, new_leads)
    else:
        print("📭 No new leads to alert — skipping email")



if __name__ == "__main__":
    main()

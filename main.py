import requests
from bs4 import BeautifulSoup
import gspread
import json
import os
import time

from google.oauth2.service_account import Credentials

# ✅ NEW: Selenium imports
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# ---------------- CONFIG ---------------- #

URL_SHEET_MAP = {
    "https://www.alpshealthcare.com.sg/strategic-procurement/national-sourcing-events/": "National Sourcing Events",
    "https://www.alpshealthcare.com.sg/strategic-procurement/pharmaceutical-sourcing-events/": "Pharmaceutical Sourcing Events",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9"
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SPREADSHEET_ID = "1ZGf468X845aw8pJ4hmdyYHsV7JrrHhZsCxq4mXLrRdg"

# ---------------- GOOGLE AUTH ---------------- #

def get_google_creds():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if not creds_json:
        raise Exception("Missing GOOGLE_CREDENTIALS_JSON")

    creds_dict = json.loads(creds_json)

    return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)


# ---------------- SCRAPER ---------------- #

def fetch(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        return res.text
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return ""


def extract_events(html, url):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    current_month = None

    for tag in soup.find_all(["h1", "h2", "h3", "h4", "table"]):

        if tag.name in ["h1", "h2", "h3", "h4"]:
            text = tag.get_text(strip=True)

            if any(month in text.lower() for month in [
                "january","february","march","april","may","june",
                "july","august","september","october","november","december"
            ]):
                current_month = text

        elif tag.name == "table":
            rows = tag.find_all("tr")

            if not rows:
                continue

            headers = [h.get_text(strip=True) for h in tag.find_all("th")]

            if not headers:
                first_row_cols = rows[0].find_all(["td", "th"])
                headers = [c.get_text(strip=True) for c in first_row_cols]
                rows = rows[1:]

            for tr in rows:
                cols = [td.get_text(strip=True) for td in tr.find_all("td")]

                if not cols or cols == headers:
                    continue

                row = {"source_url": url}

                if current_month:
                    row["PERIOD"] = current_month

                for i in range(min(len(headers), len(cols))):
                    row[headers[i]] = cols[i]

                results.append(row)

    return results


# ---------------- SELENIUM (ARIBA) ---------------- #

def init_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")

    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )


def login_ariba(driver):
    driver.get("https://supplier.ariba.com")

    print("👉 Please log in to Ariba manually...")
    input("Press ENTER after login...")


def search_ariba(driver, keyword):
    try:
        search_box = driver.find_element(By.XPATH, "//input[@type='text']")
        search_box.clear()
        search_box.send_keys(keyword)
        search_box.send_keys(Keys.RETURN)
        time.sleep(3)
    except Exception as e:
        print(f"Search error: {e}")


def extract_ariba_results(driver):
    results = []

    rows = driver.find_elements(By.XPATH, "//table//tr")

    for row in rows:
        cols = row.find_elements(By.TAG_NAME, "td")

        if len(cols) < 5:
            continue

        try:
            results.append({
                "S/N": cols[0].text.strip(),
                "url": cols[1].find_element(By.TAG_NAME, "a").get_attribute("href")
                        if cols[1].find_elements(By.TAG_NAME, "a") else "",
                "RFI ID": cols[2].text.strip(),
                "Title": cols[3].text.strip(),
                "Respond by date": cols[4].text.strip()
            })
        except:
            continue

    return results


def enrich_with_ariba(scraped_data):
    driver = init_driver()
    login_ariba(driver)

    enriched = []

    for row in scraped_data:
        keyword = (
            row.get("Description")
            or row.get("Event Name")
            or list(row.values())[0]
        )

        print(f"🔍 Searching: {keyword}")

        search_ariba(driver, keyword)
        ariba_results = extract_ariba_results(driver)

        if not ariba_results:
            enriched.append(row)
            continue

        # ✅ Take first match only (cleaner)
        combined = row.copy()
        combined.update(ariba_results[0])
        enriched.append(combined)

    driver.quit()
    return enriched


# ---------------- GOOGLE SHEETS ---------------- #

def connect_spreadsheet():
    creds = get_google_creds()
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")


def write_to_sheet(sheet, data):
    sheet.clear()

    if not data:
        return

    headers = list(data[0].keys())
    rows = [headers] + [[row.get(h, "") for h in headers] for row in data]

    sheet.update(rows)


# ---------------- MAIN ---------------- #

def main():
    spreadsheet = connect_spreadsheet()

    for url, sheet_name in URL_SHEET_MAP.items():
        print(f"Scraping: {url}")

        html = fetch(url)
        data = extract_events(html, url) if html else []

        print(f"{sheet_name}: {len(data)} rows")

        # ✅ Write raw data
        worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
        write_to_sheet(worksheet, data)

        # ✅ Enrich with Ariba
        print("🚀 Running Ariba enrichment...")
        enriched = enrich_with_ariba(data)

        ariba_sheet = get_or_create_worksheet(
            spreadsheet,
            sheet_name + " (Ariba)"
        )

        write_to_sheet(ariba_sheet, enriched)

    print("✅ Done!")


if __name__ == "__main__":
    main()

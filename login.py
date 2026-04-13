from playwright.sync_api import sync_playwright

ARIBA_URL = "https://service.ariba.com/Sourcing.aw/"
SESSION_FILE = "ariba_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto(ARIBA_URL)

    print("👉 Please login in the browser window")

    # wait long enough for login
    page.wait_for_timeout(180000)

    context.storage_state(path=SESSION_FILE)

    print("✅ Saved session")
    browser.close()

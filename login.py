from playwright.sync_api import sync_playwright

ARIBA_URL = "https://service.ariba.com/Sourcing.aw/"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.launch_persistent_context(
            user_data_dir="ariba_profile",
            headless=True
        )

        page = context.new_page()
        page.goto(ARIBA_URL)

        print("Waiting for login session...")

        # give time in case session already exists via cookies/redirect
        page.wait_for_timeout(60000)

        context.close()
        browser.close()

if __name__ == "__main__":
    main()

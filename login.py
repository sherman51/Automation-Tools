from playwright.sync_api import sync_playwright

ARIBA_URL = "https://service.ariba.com/Sourcing.aw/"

def main():
    with sync_playwright() as p:

        context = p.chromium.launch_persistent_context(
            user_data_dir="ariba_profile",
            headless=False
        )

        page = context.new_page()
        page.goto(ARIBA_URL)

        print("👉 Login manually (if needed)")

        # give time for login / session
        page.wait_for_timeout(180000)

        context.close()

if __name__ == "__main__":
    main()

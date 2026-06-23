import asyncio
import re
import aiohttp
from playwright.async_api import async_playwright

MOBILE = "7875606906"
RC_NUMBER = "UP-99-NKG-8903"

TEMP_MAIL_API = "https://api.internal.temp-mail.io/api/v3/email/z9s9ozgjfn@ozsaip.com/messages"

USER_DATA_DIR = "./chrome-session"

POLL_INTERVAL = 2
MAX_POLLS = 60


# ─── ASYNC MAIL FETCH ─────────────────────────

async def get_messages(session, api_url):
    try:
        async with session.get(api_url) as r:
            if r.status != 200:
                return []
            return await r.json()
    except:
        return []


async def get_latest_otp(session, existing_ids, api_url):
    messages = await get_messages(session, api_url)
    new_msgs = [m for m in messages if m["id"] not in existing_ids]

    for msg in new_msgs:
        combined = msg.get("subject", "") + " " + msg.get("body_text", "")
        match = re.search(r'(\d{6})', combined)
        if match:
            return match.group(1), msg["id"]

    return None, None


# ─── LOGIN CHECK ─────────────────────────────

async def is_logged_in(page):
    try:
        await page.goto("https://www.insurance.beyondsure.in/customer/dashboard")
        await page.wait_for_timeout(3000)
        return "dashboard" in page.url
    except:
        return False


# ─── LOGIN FLOW ─────────────────────────────

async def login_flow(page, session, mobile, api_url):
    print("[*] Logging in...")

    await page.goto("https://www.insurance.beyondsure.in/login")
    await page.wait_for_timeout(3000)

    mobile_input = page.locator("input").first
    await mobile_input.click()
    await page.keyboard.type(mobile, delay=100)

    await page.wait_for_timeout(1000)
    await page.locator("#send-mobile-number").click()

    print("[*] Waiting for OTP...")

    existing_msgs = await get_messages(session, api_url)
    existing_ids = {m["id"] for m in existing_msgs}

    otp = None

    for _ in range(MAX_POLLS):
        otp, _ = await get_latest_otp(session, existing_ids, api_url)
        if otp:
            print("[+] OTP:", otp)
            break
        await asyncio.sleep(POLL_INTERVAL)

    if not otp:
        print("❌ OTP not found")
        return False

    print("[*] Entering OTP...")

    await page.wait_for_timeout(3000)

    otp_inputs = page.locator("input:visible")

    for i in range(6):
        await otp_inputs.nth(i).fill(otp[i])

    print("[*] Waiting for auto login...")
    await page.wait_for_timeout(5000)

    return True


# ─── MAIN ─────────────────────────────────────

async def main():
    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:

            # Configured to run headlessly
            context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )

            page = context.pages[0] if context.pages else await context.new_page()

            if not await is_logged_in(page):
                ok = await login_flow(page, session, MOBILE, TEMP_MAIL_API)
                if not ok:
                    return

            print("[*] Opening RC page...")

            await page.goto(
                "https://www.insurance.beyondsure.in/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1"
            )

            await page.wait_for_timeout(5000)

            await page.fill("#vehicle_registration_number", RC_NUMBER)
            await page.click("#get_vahan_data")

            await page.wait_for_timeout(5000)

            cookies = await context.cookies()

            xsrf = next((c["value"] for c in cookies if c["name"] == "XSRF-TOKEN"), None)
            session_cookie = next((c["value"] for c in cookies if c["name"] == "bimasuraksha_session"), None)

            if xsrf and session_cookie:
                final = f"XSRF-TOKEN={xsrf}; bimasuraksha_session={session_cookie}"
                print("\n🔥 COOKIE:\n", final)

                with open("cookies.txt", "w") as f:
                    f.write(final)
            else:
                print("❌ Cookies not found")

            await context.close()


if __name__ == "__main__":
    asyncio.run(main())

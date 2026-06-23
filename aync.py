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
            data = await r.json()
            # Handle both list and {messages: [...]} formats
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("messages", data.get("data", data.get("emails", [])))
            return []
    except Exception as e:
        print(f"[DEBUG] Fetch error: {e}")
        return []


async def get_latest_otp(session, existing_ids, api_url):
    messages = await get_messages(session, api_url)
    new_msgs = [m for m in messages if m.get("id") not in existing_ids]

    for msg in new_msgs:
        # Check subject, body_text, body_html, and any text field
        subject = msg.get("subject", "")
        body_text = msg.get("body_text", "") or msg.get("text", "") or msg.get("body", "") or ""
        body_html = msg.get("body_html", "") or msg.get("html", "") or ""
        combined = subject + " " + body_text + " " + body_html
        
        # Only match if it's from BeyondSure
        if "beyondsure" in (subject + " " + msg.get("from", "")).lower() or "otp" in subject.lower():
            match = re.search(r'(\d{6})', combined)
            if match:
                return match.group(1), msg["id"]
    return None, None


# ─── LOGIN CHECK ─────────────────────────────

async def is_logged_in(page):
    try:
        await page.goto("https://www.insurance.beyondsure.in/customer/dashboard", timeout=30000)
        await page.wait_for_timeout(3000)
        return "dashboard" in page.url
    except:
        return False


# ─── LOGIN FLOW ─────────────────────────────

async def login_flow(page, session, mobile, api_url):
    print("[*] Logging in...")

    # Clear cookies first to avoid stale sessions
    await page.context.clear_cookies()
    
    await page.goto("https://www.insurance.beyondsure.in/login", timeout=30000)
    await page.wait_for_timeout(3000)
    
    # Save screenshot for debugging
    await page.screenshot(path=f"{USER_DATA_DIR}/login_page.png")

    mobile_input = page.locator("input#mobile-number, input[type='tel'], input:visible").first
    await mobile_input.click()
    await mobile_input.fill("")
    await page.keyboard.type(mobile, delay=100)

    await page.wait_for_timeout(1000)
    
    # Check mobile input value
    val = await mobile_input.input_value()
    print(f"[*] Mobile input value: '{val}'")
    
    # Click continue button
    continue_btn = page.locator("#send-mobile-number, button:has-text('Continue'), button:has-text('Send'), button:has-text('Get OTP')").first
    await continue_btn.click()
    
    print("[*] Waiting for OTP...")
    await page.wait_for_timeout(3000)
    
    await page.screenshot(path=f"{USER_DATA_DIR}/after_otp_request.png")

    existing_msgs = await get_messages(session, api_url)
    existing_ids = {m.get("id") for m in existing_msgs}
    print(f"[*] Existing emails: {len(existing_ids)}")

    otp = None

    for i in range(1, MAX_POLLS + 1):
        otp, msg_id = await get_latest_otp(session, existing_ids, api_url)
        if otp:
            print(f"[+] OTP found: {otp} (msg: {msg_id})")
            break
        if i % 10 == 0:
            print(f"[-] Poll {i}: no OTP yet...")
        await asyncio.sleep(POLL_INTERVAL)

    if not otp:
        print("❌ OTP not found in email")
        await page.screenshot(path=f"{USER_DATA_DIR}/otp_timeout.png")
        return False
    
    print(f"[*] Entering OTP: {otp}...")
    await page.wait_for_timeout(2000)

    # Method 1: Try individual OTP digit boxes
    try:
        otp_container = page.locator(".otp-input-container, .otp-box, .digit-group, [class*='otp']").first
        inputs = otp_container.locator("input").all()
        input_count = len(await inputs)
        print(f"[*] Found {input_count} OTP input boxes in container")
        
        if input_count >= 6:
            for i in range(6):
                await inputs[i].fill(otp[i])
            otp_filled = True
        else:
            otp_filled = False
    except:
        otp_filled = False
    
    # Method 2: Fallback - try all visible inputs
    if not otp_filled:
        print("[*] Using fallback OTP entry method...")
        all_inputs = page.locator("input:visible")
        count = await all_inputs.count()
        
        otp_digits_entered = 0
        for i in range(count):
            inp = all_inputs.nth(i)
            try:
                placeholder = await inp.get_attribute("placeholder") or ""
                type_attr = await inp.get_attribute("type") or ""
                max_length = await inp.get_attribute("maxlength") or ""
                
                # Check if this looks like an OTP input
                if "otp" in placeholder.lower() or "digit" in placeholder.lower() or max_length == "1" or type_attr == "tel":
                    if otp_digits_entered < 6:
                        await inp.fill(otp[otp_digits_entered])
                        otp_digits_entered += 1
            except:
                pass
        
        if otp_digits_entered < 6:
            print(f"[*] Only entered {otp_digits_entered}/6 digits via fallback")
            # Method 3: Try keyboard approach
            print("[*] Trying keyboard approach...")
            focused_input = all_inputs.first
            await focused_input.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.type(otp, delay=150)

    print("[*] Waiting for auto-login...")
    await page.wait_for_timeout(8000)
    
    await page.screenshot(path=f"{USER_DATA_DIR}/after_otp_entry.png")
    
    current_url = page.url
    print(f"[*] Post-login URL: {current_url}")
    
    cookies = await page.context.cookies()
    print(f"[*] Cookies after login: {len(cookies)}")
    for c in cookies:
        print(f"    {c['name']}: {c['value'][:30]}...")
    
    return "login" not in current_url


# ─── MAIN ─────────────────────────────────────

async def main():
    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=False,  # Set to False to see what's happening
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )

            page = context.pages[0] if context.pages else await context.new_page()

            if not await is_logged_in(page):
                ok = await login_flow(page, session, MOBILE, TEMP_MAIL_API)
                if not ok:
                    print("❌ Login failed")
                    await context.close()
                    return
                print("[+] Login successful!")

            print("[*] Opening RC page...")
            await page.goto(
                "https://www.insurance.beyondsure.in/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1",
                timeout=30000
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
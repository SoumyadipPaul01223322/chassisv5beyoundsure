import aiohttp
from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright
import re
import asyncio

app = FastAPI(title="Chassis Cookie Grabber API (v5)")

USER_DATA_DIR = "./chrome-session"
POLL_INTERVAL = 2
MAX_POLLS = 60

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

async def is_logged_in(page):
    try:
        await page.goto("https://www.insurance.beyondsure.in/customer/dashboard", timeout=30000)
        await page.wait_for_timeout(3000)
        return "dashboard" in page.url
    except:
        return False

async def login_flow(page, session, mobile, api_url):
    await page.goto("https://www.insurance.beyondsure.in/login", timeout=30000)
    await page.wait_for_timeout(3000)

    # Click first input and type mobile
    mobile_input = page.locator("input").first
    await mobile_input.click()
    await page.keyboard.type(mobile, delay=100)
    await page.wait_for_timeout(1000)
    
    # Request code
    await page.locator("#send-mobile-number").click()

    existing_msgs = await get_messages(session, api_url)
    existing_ids = {m["id"] for m in existing_msgs}

    otp = None
    for _ in range(MAX_POLLS):
        otp, _ = await get_latest_otp(session, existing_ids, api_url)
        if otp:
            break
        await asyncio.sleep(POLL_INTERVAL)

    if not otp:
        return False

    await page.wait_for_timeout(3000)
    otp_inputs = page.locator("input:visible")
    
    for i in range(6):
        await otp_inputs.nth(i).fill(otp[i])

    await page.wait_for_timeout(5000)
    return True

@app.get("/grab")
async def grab_cookies(
    mobile: str = Query("7875606906", description="Mobile number to login with"),
    rc_number: str = Query("UP-99-NKG-8903", description="Vehicle registration number"),
    temp_mail_api: str = Query("https://api.internal.temp-mail.io/api/v3/email/z9s9ozgjfn@ozsaip.com/messages", description="API endpoint to fetch temp email OTP")
):
    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            try:
                # Launch Chromium headlessly with sandbox disabled for Docker compatibility
                context = await p.chromium.launch_persistent_context(
                    USER_DATA_DIR,
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                )
                
                page = context.pages[0] if context.pages else await context.new_page()

                # Step 1: Check login status
                if not await is_logged_in(page):
                    success = await login_flow(page, session, mobile, temp_mail_api)
                    if not success:
                        await context.close()
                        raise HTTPException(status_code=500, detail="Failed to log in (OTP not received or login failed).")

                # Step 2: Open RC retrieval page
                await page.goto(
                    "https://www.insurance.beyondsure.in/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1",
                    timeout=30000
                )
                await page.wait_for_timeout(5000)

                # Step 3: Input vehicle registration & fetch Vahan data
                await page.fill("#vehicle_registration_number", rc_number)
                await page.click("#get_vahan_data")
                await page.wait_for_timeout(5000)

                # Step 4: Extract relevant cookies
                cookies = await context.cookies()
                xsrf = next((c["value"] for c in cookies if c["name"] == "XSRF-TOKEN"), None)
                session_cookie = next((c["value"] for c in cookies if c["name"] == "bimasuraksha_session"), None)

                await context.close()

                if xsrf and session_cookie:
                    return {
                        "success": True,
                        "cookie": f"XSRF-TOKEN={xsrf}; bimasuraksha_session={session_cookie}",
                        "details": {
                            "XSRF-TOKEN": xsrf,
                            "bimasuraksha_session": session_cookie
                        }
                    }
                else:
                    return {
                        "success": False,
                        "error": "Required cookies not found."
                    }

            except Exception as e:
                try:
                    await context.close()
                except:
                    pass
                raise HTTPException(status_code=500, detail=str(e))

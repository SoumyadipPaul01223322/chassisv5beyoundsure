import aiohttp
from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright
import re
import asyncio
import os
import sys
import traceback

app = FastAPI(
    title="Chassis Cookie Grabber API (v5)",
    description="API to automate session cookie acquisition safely."
)

USER_DATA_DIR = "./chrome-session"
POLL_INTERVAL = 2
MAX_POLLS = 60

# Fetch default configurations from environment variables to avoid exposing credentials or domains in code
DEFAULT_MOBILE = os.getenv("DEFAULT_MOBILE", "")
DEFAULT_RC_NUMBER = os.getenv("DEFAULT_RC_NUMBER", "")
DEFAULT_TEMP_MAIL_API = os.getenv("DEFAULT_TEMP_MAIL_API", "")
TARGET_URL_BASE = os.getenv("TARGET_URL_BASE", "https://www.insurance.beyondsure.in")

async def get_messages(session, api_url):
    try:
        async with session.get(api_url) as r:
            if r.status != 200:
                return []
            return await r.json()
    except Exception as e:
        print(f"[DEBUG] Failed to fetch emails: {str(e)}", file=sys.stderr)
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
        await page.goto(f"{TARGET_URL_BASE}/customer/dashboard", timeout=30000)
        await page.wait_for_timeout(3000)
        return "dashboard" in page.url
    except Exception as e:
        print(f"[DEBUG] Login check failed: {str(e)}", file=sys.stderr)
        return False

async def login_flow(page, session, mobile, api_url):
    await page.goto(f"{TARGET_URL_BASE}/login", timeout=30000)
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
        print("[DEBUG] OTP lookup timed out or failed.", file=sys.stderr)
        return False

    await page.wait_for_timeout(3000)
    otp_inputs = page.locator("input:visible")
    
    for i in range(6):
        await otp_inputs.nth(i).fill(otp[i])

    # Wait for auto-login redirect process to complete
    print("[*] Waiting for auto login redirect...")
    await page.wait_for_timeout(8000)
    
    # Confirm redirected to dashboard
    return "dashboard" in page.url

@app.get("/")
async def index():
    return {"status": "active", "service": "chassis-grabber-v5"}

@app.get("/grab")
async def grab_cookies(
    mobile: str = Query(None, description="Mobile number (optional, fallback to env)"),
    rc_number: str = Query(None, description="Vehicle registration number (optional, fallback to env)"),
    temp_mail_api: str = Query(None, description="Temp mail messages URL (optional, fallback to env)")
):
    # Resolve parameter values, prioritizing request query arguments, then system environment variables
    req_mobile = mobile or DEFAULT_MOBILE
    req_rc = rc_number or DEFAULT_RC_NUMBER
    req_mail_api = temp_mail_api or DEFAULT_TEMP_MAIL_API

    if not req_mobile or not req_rc or not req_mail_api:
        raise HTTPException(
            status_code=400,
            detail="Missing configuration. Parameters must be passed via query string or environment variables."
        )

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            context = None
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
                    success = await login_flow(page, session, req_mobile, req_mail_api)
                    if not success:
                        raise Exception("Verification flow incomplete or invalid OTP.")

                # Step 2: Open RC retrieval page
                await page.goto(
                    f"{TARGET_URL_BASE}/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1",
                    timeout=30000
                )
                await page.wait_for_timeout(5000)

                # Step 3: Input vehicle registration & fetch Vahan data
                await page.fill("#vehicle_registration_number", req_rc)
                await page.click("#get_vahan_data")
                await page.wait_for_timeout(10000)

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
                        "error": "Grab verification failed: session cookies were missing."
                    }

            except Exception as err:
                # Print exception trace to standard error for Docker dashboard logs (hidden from client)
                print("[ERROR] Internal error captured in Grab pipeline:", file=sys.stderr)
                traceback.print_exc()

                if context:
                    try:
                        await context.close()
                    except:
                        pass
                
                # Expose a generic, sanitized response to prevent leaking endpoints or internal codes
                raise HTTPException(
                    status_code=500,
                    detail="An error occurred while executing the automation sequence. Please consult system logs."
                )

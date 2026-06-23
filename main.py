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
    print(f"[*] Navigating to {TARGET_URL_BASE}/login...", file=sys.stderr, flush=True)
    await page.goto(f"{TARGET_URL_BASE}/login", timeout=30000)
    await page.wait_for_timeout(3000)

    # Click first input and type mobile
    print(f"[*] Typing mobile number: {mobile}...", file=sys.stderr, flush=True)
    mobile_input = page.locator("input").first
    await mobile_input.click()
    await page.keyboard.type(mobile, delay=100)
    await page.wait_for_timeout(1000)
    
    # Request code
    print("[*] Requesting OTP code...", file=sys.stderr, flush=True)
    await page.locator("#send-mobile-number").click()

    print("[*] Fetching initial emails to establish baseline...", file=sys.stderr, flush=True)
    existing_msgs = await get_messages(session, api_url)
    existing_ids = {m["id"] for m in existing_msgs}
    print(f"[*] Baseline established. Found {len(existing_ids)} existing email(s).", file=sys.stderr, flush=True)

    otp = None
    print("[*] Starting OTP polling loop...", file=sys.stderr, flush=True)
    for poll_idx in range(1, MAX_POLLS + 1):
        print(f"    -> Polling for OTP (attempt {poll_idx}/{MAX_POLLS})...", file=sys.stderr, flush=True)
        otp, _ = await get_latest_otp(session, existing_ids, api_url)
        if otp:
            print(f"[*] OTP received successfully: {otp}", file=sys.stderr, flush=True)
            break
        await asyncio.sleep(POLL_INTERVAL)

    if not otp:
        print("[DEBUG] OTP lookup timed out or failed.", file=sys.stderr, flush=True)
        return False

    await page.wait_for_timeout(3000)
    otp_inputs = page.locator("input:visible")
    
    print("[*] Filling OTP into inputs...", file=sys.stderr, flush=True)
    for i in range(6):
        await otp_inputs.nth(i).fill(otp[i])

    # Wait for auto-login redirect process to complete
    print("[*] Waiting for auto login redirect...", file=sys.stderr, flush=True)
    await page.wait_for_timeout(8000)
    
    # Confirm login succeeded: page should no longer be on /login
    current_url = page.url
    print(f"[*] Post-login URL: {current_url}", file=sys.stderr, flush=True)
    return "login" not in current_url

@app.get("/")
async def index():
    return {"status": "active", "service": "chassis-grabber-v5"}

@app.get("/grab")
async def grab_cookies(
    mobile: str = Query(None, description="Mobile number (optional, fallback to env)"),
    rc_number: str = Query(None, description="Vehicle registration number (optional, fallback to env)"),
    temp_mail_api: str = Query(None, description="Temp mail messages URL (optional, fallback to env)")
):
    from urllib.parse import urlparse, unquote

    # Resolve parameter values
    req_mobile = mobile or DEFAULT_MOBILE
    req_rc = rc_number or DEFAULT_RC_NUMBER
    req_mail_api = temp_mail_api or DEFAULT_TEMP_MAIL_API

    print(f"[*] grab_cookies invoked with mobile={req_mobile}, rc={req_rc}, api={req_mail_api}", file=sys.stderr, flush=True)

    if not req_mobile or not req_rc or not req_mail_api:
        raise HTTPException(
            status_code=400,
            detail="Missing configuration. Parameters must be passed via query string or environment variables."
        )

    RC_PAGE_URL = f"{TARGET_URL_BASE}/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1"

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            context = None
            try:
                context = await p.chromium.launch_persistent_context(
                    USER_DATA_DIR,
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                )
                
                page = context.pages[0] if context.pages else await context.new_page()

                # Step 1: Go DIRECTLY to RC page (skip dashboard check for speed)
                print("[*] Navigating directly to RC page...", file=sys.stderr, flush=True)
                await page.goto(RC_PAGE_URL, timeout=30000, wait_until="networkidle")
                await page.wait_for_timeout(2000)

                # Step 2: If redirected to login page, perform login then come back
                if "login" in page.url:
                    print("[*] Session expired — performing login...", file=sys.stderr, flush=True)
                    success = await login_flow(page, session, req_mobile, req_mail_api)
                    if not success:
                        raise Exception("Verification flow incomplete or invalid OTP.")
                    # After login, navigate to RC page with full load
                    await page.goto(RC_PAGE_URL, timeout=30000, wait_until="networkidle")
                    await page.wait_for_timeout(3000)

                print(f"[*] On page: {page.url}", file=sys.stderr, flush=True)

                # Step 3: Capture VALID cookies NOW (before any AJAX calls that could invalidate them)
                pre_click_cookies = await context.cookies([TARGET_URL_BASE])
                if not pre_click_cookies:
                    print("[*] context.cookies(TARGET_URL_BASE) returned nothing, fetching all cookies...", file=sys.stderr, flush=True)
                    pre_click_cookies = await context.cookies()
                print(f"[*] Pre-click cookies: {len(pre_click_cookies)} found", file=sys.stderr, flush=True)
                for c in pre_click_cookies:
                    print(f"    -> {c['name']} = {c['value'][:40]}...", file=sys.stderr, flush=True)

                xsrf = next((c["value"] for c in pre_click_cookies if c["name"] == "XSRF-TOKEN"), None)
                session_cookie = next((c["value"] for c in pre_click_cookies if c["name"] == "bimasuraksha_session"), None)

                # Fallback: try document.cookie if context.cookies() failed
                if not xsrf or not session_cookie:
                    print("[*] Cookies missing from context, trying document.cookie...", file=sys.stderr, flush=True)
                    try:
                        js_cookies = await page.evaluate("document.cookie")
                        print(f"[*] document.cookie: {js_cookies[:120]}...", file=sys.stderr, flush=True)
                        for pair in js_cookies.split(";"):
                            pair = pair.strip()
                            if pair.startswith("XSRF-TOKEN=") and not xsrf:
                                xsrf = pair.split("=", 1)[1]
                            elif pair.startswith("bimasuraksha_session=") and not session_cookie:
                                session_cookie = pair.split("=", 1)[1]
                    except Exception as e:
                        print(f"[*] document.cookie fallback failed: {e}", file=sys.stderr, flush=True)

                # Step 4: Extract enquiry_id and form data from page JS (without clicking)
                enquiry_id = ""
                try:
                    enquiry_id = await page.evaluate("""
                        () => {
                            // Try common ways the enquiry_id might be stored
                            if (typeof enquiry_id !== 'undefined') return enquiry_id;
                            const el = document.querySelector('#enquiry_id, input[name="enquiry_id"]');
                            if (el) return el.value;
                            return '';
                        }
                    """)
                    print(f"[*] Extracted enquiry_id: {enquiry_id}", file=sys.stderr, flush=True)
                except:
                    pass

                # Step 5: Set up network interception, fill RC and click to get full request data
                captured_request = {}
                vahan_response_event = asyncio.Event()

                async def handle_request(request):
                    if "get_vahan_service" in request.url:
                        print(f"[*] Intercepted request: {request.method} {request.url}", file=sys.stderr, flush=True)
                        captured_request["url"] = request.url
                        captured_request["method"] = request.method
                        captured_request["headers"] = await request.all_headers()
                        captured_request["post_data"] = request.post_data

                async def handle_response(response):
                    if "get_vahan_service" in response.url:
                        print(f"[*] Got response: {response.status} from {response.url}", file=sys.stderr, flush=True)
                        captured_request["response_status"] = response.status
                        vahan_response_event.set()

                page.on("request", handle_request)
                page.on("response", handle_response)

                # Fill RC number and click
                await page.fill("#vehicle_registration_number", req_rc)
                await page.click("#get_vahan_data")

                # Wait for the Vahan response (max 15 seconds)
                try:
                    await asyncio.wait_for(vahan_response_event.wait(), timeout=15)
                    print("[*] Vahan response received!", file=sys.stderr, flush=True)
                except asyncio.TimeoutError:
                    print("[*] Vahan response timed out, continuing...", file=sys.stderr, flush=True)

                # Step 6: RESTORE pre-click cookies ONLY if the response was a 401
                response_status = captured_request.get("response_status")
                if response_status == 401:
                    print("[*] Vahan response was 401. Restoring pre-click cookies to protect session.", file=sys.stderr, flush=True)
                    await context.clear_cookies()
                    for c in pre_click_cookies:
                        await context.add_cookies([c])
                else:
                    print(f"[*] Vahan response was {response_status}. Keeping latest rotated cookies to maintain session.", file=sys.stderr, flush=True)

                await context.close()

                if captured_request.get("headers"):
                    headers = dict(captured_request["headers"])
                    method = captured_request.get("method", "POST")
                    url = captured_request.get("url", "")
                    post_data = captured_request.get("post_data", "")
                    
                    # Try to extract cookie string from headers (case-insensitive)
                    cookie_str = ""
                    cookie_key = "cookie"
                    for k, v in headers.items():
                        if k.lower() == "cookie":
                            cookie_str = v
                            cookie_key = k
                            break

                    # Fall back to pre-click cookies if request headers didn't contain it
                    if not cookie_str:
                        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in pre_click_cookies)
                        if cookie_str:
                            headers["cookie"] = cookie_str
                            cookie_key = "cookie"

                    # Parse values from cookie string
                    xsrf_val = xsrf
                    session_val = session_cookie
                    if cookie_str:
                        for pair in cookie_str.split(";"):
                            pair = pair.strip()
                            if pair.startswith("XSRF-TOKEN="):
                                xsrf_val = pair.split("=", 1)[1]
                            elif pair.startswith("bimasuraksha_session="):
                                session_val = pair.split("=", 1)[1]

                    # Laravel X-XSRF-TOKEN header check
                    has_xsrf_header = any(k.lower() == "x-xsrf-token" for k in headers.keys())
                    if not has_xsrf_header and xsrf_val:
                        headers["x-xsrf-token"] = unquote(xsrf_val)

                    # Add Host and Origin
                    parsed = urlparse(url)
                    host = parsed.netloc
                    if not any(k.lower() == "host" for k in headers.keys()):
                        headers["host"] = host
                    if not any(k.lower() == "origin" for k in headers.keys()):
                        headers["origin"] = f"{parsed.scheme}://{host}"

                    path = parsed.path
                    if parsed.query:
                        path += f"?{parsed.query}"

                    # Build raw header block
                    raw_lines = [f"{method} {path} HTTP/1.1"]
                    for key, value in headers.items():
                        raw_lines.append(f"{key}: {value}")
                    raw_header = "\r\n".join(raw_lines)

                    return {
                        "success": True,
                        "raw_request_header": raw_header,
                        "method": method,
                        "url": url,
                        "headers": headers,
                        "post_data": post_data,
                        "cookie": cookie_str,
                        "details": {
                            "XSRF-TOKEN": xsrf_val,
                            "bimasuraksha_session": session_val
                        }
                    }
                elif xsrf and session_cookie:
                    return {
                        "success": True,
                        "cookie": f"XSRF-TOKEN={xsrf}; bimasuraksha_session={session_cookie}",
                        "details": {
                            "XSRF-TOKEN": xsrf,
                            "bimasuraksha_session": session_cookie
                        },
                        "note": "Vahan service request was not intercepted. Returning browser cookies only."
                    }
                else:
                    return {
                        "success": False,
                        "error": "Grab verification failed: session cookies were missing."
                    }

            except Exception as err:
                print("[ERROR] Internal error captured in Grab pipeline:", file=sys.stderr)
                traceback.print_exc()

                if context:
                    try:
                        await context.close()
                    except:
                        pass
                
                raise HTTPException(
                    status_code=500,
                    detail="An error occurred while executing the automation sequence. Please consult system logs."
                )


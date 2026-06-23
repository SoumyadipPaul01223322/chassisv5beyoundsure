import aiohttp
from fastapi import FastAPI, HTTPException, Query
from playwright.async_api import async_playwright
import re
import asyncio
import os
import sys
import traceback
import random

app = FastAPI(
    title="Chassis Cookie Grabber API (v5)",
    description="API to automate session cookie acquisition safely."
)

USER_DATA_DIR = "./chrome-session"

PROXIES = [
    {"server": "http://9.142.215.9:6174", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://72.46.138.234:6460", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://82.21.55.145:7409", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://46.202.34.70:7836", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://45.58.229.64:5236", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://82.29.47.181:7905", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://72.1.153.149:5541", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://185.253.122.165:5974", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://192.46.201.254:6768", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"},
    {"server": "http://216.98.230.63:6516", "username": "xovmkdcfstaticresidential", "password": "2ook6qr067sf"}
]
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

async def print_current_cookies(context, label):
    try:
        cookies = await context.cookies()
        print(f"[*] [COOKIES] {label} (context ID: {id(context)}): {len(cookies)} cookies in context", file=sys.stderr, flush=True)
        for c in cookies:
            expires = c.get('expires', 'Session')
            print(f"    -> {c['name']} = {c['value'][:30]}... | domain={c.get('domain')} | path={c.get('path')} | expires={expires} | secure={c.get('secure')} | httpOnly={c.get('httpOnly')}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[*] [COOKIES] Failed to fetch cookies at {label}: {e}", file=sys.stderr, flush=True)

async def is_logged_in(page):
    try:
        await page.goto(f"{TARGET_URL_BASE}/customer/dashboard", timeout=30000)
        await page.wait_for_timeout(3000)
        return "dashboard" in page.url
    except Exception as e:
        print(f"[DEBUG] Login check failed: {str(e)}", file=sys.stderr)
        return False

async def login_flow(page, session, mobile, api_url):
    await print_current_cookies(page.context, "Start of login_flow (before clear)")
    
    # Clear cookies, localStorage, and sessionStorage to ensure a 100% clean session start.
    print("[*] Performing clean session reset (cookies, localStorage, sessionStorage)...", file=sys.stderr, flush=True)
    try:
        await page.context.clear_cookies()
        # Go to login page first so we are on the correct origin to clear localStorage/sessionStorage
        await page.goto(f"{TARGET_URL_BASE}/login", timeout=30000, wait_until="networkidle")
        await page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        # Reload the page to load it with a completely clean context
        await page.reload(wait_until="networkidle")
    except Exception as reset_err:
        print(f"[!] Warning during session reset: {reset_err}", file=sys.stderr, flush=True)
        # Fallback reload
        try:
            await page.goto(f"{TARGET_URL_BASE}/login", timeout=30000, wait_until="networkidle")
        except:
            pass
    
    await print_current_cookies(page.context, "Start of login_flow (after clean reset)")
    await page.wait_for_timeout(3000)
    
    # Save screenshot of the login page for debugging
    try:
        await page.screenshot(path=os.path.join(USER_DATA_DIR, "login_page.png"))
    except:
        pass

    # Click mobile input and type number
    print(f"[*] Typing mobile number: {mobile}...", file=sys.stderr, flush=True)
    mobile_input = page.locator("input#mobile-number, input[placeholder*='mobile'], input[placeholder*='phone'], input[placeholder*='Mobile'], input[type='tel'], input:visible").first
    await mobile_input.click()
    await mobile_input.fill("")
    await page.keyboard.type(mobile, delay=100)
    await page.wait_for_timeout(1000)
    
    # Verify input value
    typed_val = await mobile_input.input_value()
    print(f"[*] [DEBUG] Mobile input value after typing: '{typed_val}'", file=sys.stderr, flush=True)

    # Check button status
    btn = page.locator("#send-mobile-number, button:has-text('Continue'), button:has-text('Send'), button:has-text('Get OTP'), button.btn-success").first
    btn_visible = await btn.is_visible()
    btn_enabled = await btn.is_enabled()
    print(f"[*] [DEBUG] Continue button: visible={btn_visible}, enabled={btn_enabled}", file=sys.stderr, flush=True)

    # Request code
    print("[*] Requesting OTP code...", file=sys.stderr, flush=True)
    
    # Listen for response to check if it 419s
    response_future = asyncio.get_event_loop().create_future()
    
    async def check_otp_response(response):
        if response.url.endswith('/login') and response.status == 419:
            response_future.set_result(True)
    
    page.on("response", check_otp_response)
    
    await btn.click()
    await page.wait_for_timeout(3000)
    
    # Check if we got a 419
    if not response_future.done():
        response_future.set_result(False)
    
    got_419 = await response_future
    if got_419:
        print("[!] Got 419 CSRF mismatch! Refreshing page and retrying...", file=sys.stderr, flush=True)
        await page.goto(f"{TARGET_URL_BASE}/login", timeout=30000, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        
        # Retry typing
        mobile_input = page.locator("input#mobile-number, input[placeholder*='mobile'], input[type='tel'], input:visible").first
        await mobile_input.click()
        await mobile_input.fill("")
        await page.keyboard.type(mobile, delay=100)
        await page.wait_for_timeout(1000)
        
        btn = page.locator("#send-mobile-number, button:has-text('Continue'), button:has-text('Send'), button:has-text('Get OTP'), button.btn-success").first
        await btn.click()
        await page.wait_for_timeout(3000)
    
    # Save screenshot after clicking
    try:
        await page.screenshot(path=os.path.join(USER_DATA_DIR, "login_clicked.png"))
    except:
        pass

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
        try:
            screenshot_path = os.path.join(USER_DATA_DIR, "login_timeout_error.png")
            await page.screenshot(path=screenshot_path)
            print(f"[*] Saved login timeout screenshot to {screenshot_path}", file=sys.stderr, flush=True)
        except:
            pass
        return False

    # Fill OTP
    print("[*] Filling OTP into inputs...", file=sys.stderr, flush=True)
    await page.wait_for_timeout(2000)
    
    # Try finding OTP container first
    otp_filled = False
    try:
        otp_container = page.locator(".otp-input, .otp-box, .digit-group, [class*='otp'] input, input[maxlength='1']").first
        if await otp_container.is_visible():
            all_digit_inputs = page.locator("input[maxlength='1'], input.otp-input, input.form-control[type='tel']")
            count = await all_digit_inputs.count()
            if count >= 6:
                for i in range(6):
                    await all_digit_inputs.nth(i).fill(otp[i])
                otp_filled = True
                print(f"[*] Filled {count} OTP digit inputs")
    except:
        pass
    
    if not otp_filled:
        # Fallback: keyboard type
        print("[*] Using keyboard fallback for OTP...")
        try:
            first_input = page.locator("input:visible").first
            await first_input.click()
            await first_input.fill("")
            await page.keyboard.type(otp, delay=100)
        except Exception as e:
            print(f"[*] Keyboard fallback failed: {e}")
            # Last resort: try filling all visible inputs sequentially
            all_vis = page.locator("input:visible")
            cnt = await all_vis.count()
            for i in range(min(cnt, 6)):
                try:
                    await all_vis.nth(i).fill(otp[i])
                except:
                    pass

    # Wait for auto-login
    print("[*] Waiting for auto login redirect...", file=sys.stderr, flush=True)
    await page.wait_for_timeout(10000)
    
    current_url = page.url
    print(f"[*] Post-login URL: {current_url}", file=sys.stderr, flush=True)
    await print_current_cookies(page.context, "Post-login redirect completed")
    
    # Save debug screenshot
    try:
        await page.screenshot(path=os.path.join(USER_DATA_DIR, "after_login.png"))
    except:
        pass
    
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

    import datetime
    print(f"[*] grab_cookies invoked with mobile={req_mobile}, rc={req_rc}, api={req_mail_api}", file=sys.stderr, flush=True)
    print(f"[*] Container System Time: {datetime.datetime.now(datetime.timezone.utc)}", file=sys.stderr, flush=True)

    if not req_mobile or not req_rc or not req_mail_api:
        raise HTTPException(
            status_code=400,
            detail="Missing configuration. Parameters must be passed via query string or environment variables."
        )

    RC_PAGE_URL = f"{TARGET_URL_BASE}/leads/create/online?insurance_category_id=2&product_category=motor&product_type_id=3&policy_type_id=1&lead_flow=1"

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as p:
            context = None
            page = None
            console_logs = []
            try:
                use_proxy = os.getenv("USE_PROXY", "false").lower() == "true"
                launch_args = {
                    "headless": True,
                    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "ignore_https_errors": True,
                    "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                }
                if use_proxy and PROXIES:
                    selected_proxy = random.choice(PROXIES)
                    launch_args["proxy"] = selected_proxy
                    print(f"[*] Launching browser with proxy: {selected_proxy['server']}", file=sys.stderr, flush=True)
                else:
                    print("[*] Launching browser WITHOUT proxy", file=sys.stderr, flush=True)

                context = await p.chromium.launch_persistent_context(
                    USER_DATA_DIR,
                    **launch_args
                )
                
                page = context.pages[0] if context.pages else await context.new_page()

                # Pipe browser console messages to container output for visibility into client errors
                def handle_console(msg):
                    log_line = f"[{msg.type}] {msg.text}"
                    console_logs.append(log_line)
                    print(f"[BROWSER CONSOLE] {log_line}", file=sys.stderr, flush=True)
                page.on("console", handle_console)

                async def handle_response_logging(response):
                    if response.status >= 400:
                        log_line = f"[HTTP {response.status}] {response.url}"
                        console_logs.append(log_line)
                        print(log_line, file=sys.stderr, flush=True)
                    try:
                        headers = await response.all_headers()
                        set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie")
                        if set_cookie:
                            log_line = f"[SET-COOKIE] {response.url} set: {set_cookie}"
                            console_logs.append(log_line)
                            print(log_line, file=sys.stderr, flush=True)
                    except:
                        pass
                page.on("response", handle_response_logging)

                # Load manually saved cookies to bypass browser-session cookie deletion
                cookies_file = os.path.join(USER_DATA_DIR, "cookies.json")
                if os.path.exists(cookies_file):
                    try:
                        import json
                        with open(cookies_file, "r") as f:
                            saved_cookies = json.load(f)
                        if saved_cookies:
                            await context.add_cookies(saved_cookies)
                            print(f"[*] Injected {len(saved_cookies)} manually saved cookies from disk.", file=sys.stderr, flush=True)
                    except Exception as e:
                        print(f"[*] Failed to load manually saved cookies: {e}", file=sys.stderr, flush=True)

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
                await print_current_cookies(page.context, "After arriving on RC page")

                # Step 3: Capture VALID cookies NOW (before any AJAX calls that could invalidate them)
                pre_click_cookies = await page.context.cookies([TARGET_URL_BASE])
                if not pre_click_cookies:
                    print("[*] page.context.cookies(TARGET_URL_BASE) returned nothing, fetching all cookies...", file=sys.stderr, flush=True)
                    pre_click_cookies = await page.context.cookies()
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

                # Save all current cookies manually to disk to bypass browser-session deletion
                try:
                    import json
                    all_cookies = await context.cookies()
                    os.makedirs(USER_DATA_DIR, exist_ok=True)
                    with open(cookies_file, "w") as f:
                        json.dump(all_cookies, f)
                    print(f"[*] Successfully saved {len(all_cookies)} cookies to cookies.json", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"[*] Failed to save cookies manually: {e}", file=sys.stderr, flush=True)

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

                err_msg = str(err)
                tb_msg = traceback.format_exc()
                page_url = ""
                page_text = ""
                
                if page:
                    try:
                        page_url = page.url
                        page_text = await page.evaluate("() => document.body.innerText")
                    except Exception as diag_err:
                        print(f"[*] Failed to gather page diagnostics: {diag_err}", file=sys.stderr)

                if context:
                    try:
                        await context.close()
                    except:
                        pass
                
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": err_msg,
                        "traceback": tb_msg,
                        "current_url": page_url,
                        "page_text": page_text,
                        "console_logs": console_logs
                    }
                )


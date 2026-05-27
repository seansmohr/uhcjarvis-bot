"""
Core Playwright logic for UHC Jarvis Book of Business export.
Runs headless on Railway. Session is loaded from JARVIS_SESSION_B64 env var.

setup_session(mfa_fn) — call once via /setup to establish a session without
                        needing any local tools. mfa_fn() blocks until the
                        user submits their code through the web UI.
"""

import base64
import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://www.uhcjarvis.com"
JARVIS_SIGNIN_URL = "https://www.uhcjarvis.com/content/jarvis/en/sign_in.html"
SSO_URL = "https://identity.onehealthcareid.com/oneapp/index.html"
SESSION_FILE = Path("/tmp/jarvis_session.json")
DOWNLOAD_DIR = Path("/tmp/jarvis_downloads")

# Stores the most recent debug screenshot bytes — served by /setup/debug
debug_screenshot: bytes = None

# Mimic a real Mac Chrome to avoid bot-detection on the SSO page
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_STEALTH_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"


def _stealth_context(browser, **kwargs):
    """Return a new context with bot-detection mitigations applied."""
    ctx = browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        **kwargs,
    )
    ctx.add_init_script(_STEALTH_SCRIPT)
    return ctx


def _stealth_browser(playwright):
    """Launch Chromium with automation flags removed."""
    return playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )


class SessionExpiredError(Exception):
    pass


def _load_session():
    b64 = os.getenv("JARVIS_SESSION_B64", "").strip()
    if not b64:
        raise RuntimeError(
            "JARVIS_SESSION_B64 is not set. Run seed_session.py locally to generate it."
        )
    SESSION_FILE.write_bytes(base64.b64decode(b64))


def _is_logged_in(page) -> bool:
    url = page.url.lower()
    return "login" not in url and "signin" not in url and "auth" not in url


def _navigate_to_bob(page) -> bool:
    bob_urls = [
        f"{LOGIN_URL}/bob",
        f"{LOGIN_URL}/book-of-business",
        f"{LOGIN_URL}/reports/book-of-business",
        f"{LOGIN_URL}/agent/book-of-business",
    ]
    for url in bob_urls:
        try:
            page.goto(url, wait_until="networkidle", timeout=12_000)
            if _is_logged_in(page):
                return True
        except PlaywrightTimeoutError:
            continue

    nav_patterns = [
        "a:has-text('Book of Business')",
        "a:has-text('Book Of Business')",
        "a[href*='book']",
        "a[href*='bob']",
        "*[role='menuitem']:has-text('Book')",
    ]
    for pat in nav_patterns:
        try:
            page.locator(pat).first.click(timeout=5_000)
            page.wait_for_load_state("networkidle", timeout=12_000)
            if _is_logged_in(page):
                return True
        except PlaywrightTimeoutError:
            continue

    return False


def _trigger_download(page):
    export_patterns = [
        "button:has-text('Export')",
        "button:has-text('Download')",
        "a:has-text('Export')",
        "a:has-text('Download')",
        "button:has-text('Export to CSV')",
        "button:has-text('Export to Excel')",
        "*[aria-label*='export' i]",
        "*[title*='export' i]",
    ]
    for pat in export_patterns:
        try:
            loc = page.locator(pat).first
            loc.wait_for(state="visible", timeout=5_000)
            with page.expect_download(timeout=60_000) as dl_info:
                loc.click()
            return dl_info.value
        except PlaywrightTimeoutError:
            continue
    return None


def _send_email(filepath: Path = None, subject: str = None, body: str = None):
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USERNAME")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    to_addr = os.getenv("EMAIL_TO")

    if not all([smtp_user, smtp_pass, to_addr]):
        print("[email] SMTP not configured — skipping.")
        return

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject or "Jarvis Export Complete"
    msg.set_content(body or "Your Book of Business export is attached.")

    if filepath and filepath.exists():
        data = filepath.read_bytes()
        if filepath.suffix.lower() == ".csv":
            maintype, subtype = "text", "csv"
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filepath.name)

    with smtplib.SMTP(smtp_server, smtp_port) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)

    print(f"[email] Sent to {to_addr}")


def run_export() -> str:
    """Run the full export. Returns path to downloaded file."""
    _load_session()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = _stealth_browser(p)
        context = _stealth_context(browser, storage_state=str(SESSION_FILE), accept_downloads=True)
        page = context.new_page()

        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

        if not _is_logged_in(page):
            context.close()
            browser.close()
            _send_email(
                subject="Jarvis Export — Session Expired",
                body=(
                    "Your UHC Jarvis session has expired and the export could not run.\n\n"
                    "To fix:\n"
                    "1. Run seed_session.py on your local machine.\n"
                    "2. Copy the JARVIS_SESSION_B64 value it prints.\n"
                    "3. Update the JARVIS_SESSION_B64 variable in Railway and redeploy."
                ),
            )
            raise SessionExpiredError(
                "Session expired. Visit /setup on your Railway app to re-authenticate."
            )

        if not _navigate_to_bob(page):
            context.close()
            browser.close()
            raise RuntimeError(
                "Could not navigate to Book of Business. The portal UI may have changed."
            )

        download = _trigger_download(page)
        if not download:
            context.close()
            browser.close()
            raise RuntimeError(
                "Could not trigger export. The export button may have changed."
            )

        name = download.suggested_filename or f"jarvis_bob_{int(time.time())}"
        dest = DOWNLOAD_DIR / name
        download.save_as(dest)
        print(f"[export] Downloaded: {dest}")

        context.close()
        browser.close()

    _send_email(
        filepath=dest,
        subject=f"Jarvis Book of Business — {dest.name}",
        body=f"Your Book of Business export is attached.\nFile: {dest.name}",
    )

    return str(dest)


def setup_session(mfa_fn) -> str:
    """
    Establish a fresh Jarvis session. Exact login flow based on observed pages:
      1. Jarvis sign-in page → click "Sign in with One Healthcare ID"
      2. SSO: enter username → Continue
      3. SSO: enter password → Continue
      4. "Verify Your Identity" → click "Via Text Message"
      5. "Access Code" → check "Skip this step" → wait for user OTP → Continue
      6. Redirects back to uhcjarvis.com → save session
    """
    username = os.getenv("JARVIS_USERNAME")
    password = os.getenv("JARVIS_PASSWORD")
    if not username or not password:
        raise RuntimeError("JARVIS_USERNAME and JARVIS_PASSWORD must be set in Railway variables.")

    def _wait_load(pg, timeout=12_000):
        try:
            pg.wait_for_load_state("load", timeout=timeout)
        except PlaywrightTimeoutError:
            pass
        pg.wait_for_timeout(1_000)

    def _snap():
        import jarvis_export as _m
        _m.debug_screenshot = page.screenshot(full_page=True)

    with sync_playwright() as p:
        browser = _stealth_browser(p)
        context = _stealth_context(browser)
        page = context.new_page()

        # ── 1. Jarvis sign-in page ─────────────────────────────────────────────
        page.goto(JARVIS_SIGNIN_URL, wait_until="load", timeout=30_000)
        page.wait_for_timeout(2_000)
        _snap()

        try:
            page.locator(
                "button:has-text('Sign in with One Healthcare ID'), "
                "a:has-text('Sign in with One Healthcare ID')"
            ).first.click(timeout=10_000)
        except PlaywrightTimeoutError:
            _snap()
            raise RuntimeError(
                f"'Sign in with One Healthcare ID' button not found on {JARVIS_SIGNIN_URL}."
            )

        # ── 2. SSO page: username ──────────────────────────────────────────────
        try:
            page.wait_for_url("*onehealthcareid.com*", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        if "onehealthcareid.com" not in page.url:
            _snap()
            raise RuntimeError(f"Did not reach SSO. Currently at: {page.url}")

        try:
            page.wait_for_selector("input", state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            _snap()
            raise RuntimeError(f"SSO loaded but no input appeared. URL: {page.url}")

        page.wait_for_timeout(500)
        _snap()
        page.locator("input:visible").first.fill(username)
        page.locator("button:has-text('Continue')").first.click(timeout=10_000)
        _wait_load(page)

        # ── 3. SSO page: password ──────────────────────────────────────────────
        try:
            page.wait_for_selector("input[type='password']", state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            _snap()
            raise RuntimeError(f"Password field did not appear. URL: {page.url}")

        _snap()
        page.locator("input[type='password']").first.fill(password)
        page.locator("button:has-text('Continue')").first.click(timeout=10_000)
        _wait_load(page)

        # ── 4. "Verify Your Identity" page: click "Via Text Message" ──────────
        try:
            page.wait_for_selector(
                "button:has-text('Via Text Message'), button:has-text('Via Call')",
                state="visible", timeout=15_000
            )
        except PlaywrightTimeoutError:
            _snap()
            raise RuntimeError(
                f"'Verify Your Identity' page did not appear. URL: {page.url} — "
                "check /setup/debug for a screenshot."
            )

        _snap()
        page.locator("button:has-text('Via Text Message')").first.click(timeout=10_000)
        _wait_load(page)

        # ── 5. "Access Code" page: OTP entry ──────────────────────────────────
        try:
            page.wait_for_selector("input", state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            _snap()
            raise RuntimeError(f"OTP input did not appear. URL: {page.url}")

        _snap()

        # Check "Skip this step in future if this is your private device"
        # so that subsequent setups don't require OTP
        try:
            cb = page.locator("input[type='checkbox']").first
            cb.wait_for(state="visible", timeout=3_000)
            if not cb.is_checked():
                cb.check()
        except PlaywrightTimeoutError:
            pass

        # Wait for user to supply the OTP via /setup/mfa
        otp = mfa_fn()
        page.locator("input:visible").first.fill(str(otp))
        page.locator("button:has-text('Continue')").first.click(timeout=10_000)
        _wait_load(page, timeout=20_000)

        # ── 6. Should now be on Jarvis ─────────────────────────────────────────
        try:
            page.wait_for_url("*uhcjarvis.com*", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        _snap()

        if "onehealthcareid.com" in page.url:
            context.close()
            browser.close()
            raise RuntimeError(
                f"Still on SSO after entering OTP ({page.url}). "
                "Visit /setup/debug for a screenshot of what the browser sees."
            )

        context.storage_state(path=str(SESSION_FILE))
        b64 = base64.b64encode(SESSION_FILE.read_bytes()).decode()
        os.environ["JARVIS_SESSION_B64"] = b64

        context.close()
        browser.close()

    return b64

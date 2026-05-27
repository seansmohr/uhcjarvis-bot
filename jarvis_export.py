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
    Establish a fresh session via headless login.
    mfa_fn() is called when the MFA page is detected; it should block
    until the user submits their code and return it as a string.
    Returns the base64-encoded session string.
    """
    username = os.getenv("JARVIS_USERNAME")
    password = os.getenv("JARVIS_PASSWORD")
    if not username or not password:
        raise RuntimeError("JARVIS_USERNAME and JARVIS_PASSWORD must be set in Railway variables.")

    with sync_playwright() as p:
        browser = _stealth_browser(p)
        context = _stealth_context(browser)
        page = context.new_page()

        # Step 1: load the Jarvis sign-in page
        page.goto(JARVIS_SIGNIN_URL, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(2_000)

        # Step 2: click "Sign in with One Healthcare ID" — this initialises the
        #         OAuth flow and redirects to the SSO page with proper state tokens
        try:
            page.locator(
                "button:has-text('Sign in with One Healthcare ID'), "
                "a:has-text('Sign in with One Healthcare ID')"
            ).first.click(timeout=10_000)
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "Could not find the 'Sign in with One Healthcare ID' button on "
                f"{JARVIS_SIGNIN_URL}. The page layout may have changed."
            )

        # Step 3: wait to land on the SSO page (redirect may take a moment)
        try:
            page.wait_for_url("*onehealthcareid.com*", timeout=20_000)
        except PlaywrightTimeoutError:
            pass  # already there — wait_for_url can fire late on SPA hash routes

        if "onehealthcareid.com" not in page.url:
            raise RuntimeError(
                f"Did not reach OneHealthcareID after clicking sign-in. "
                f"Currently at: {page.url}"
            )

        page.wait_for_load_state("networkidle", timeout=15_000)

        # ── Username step ──────────────────────────────────────────────────────
        # OneHealthcareID uses a two-step flow:
        #   1. Enter username / email → click Continue
        #   2. Enter password → click Continue
        try:
            page.wait_for_selector("input", state="visible", timeout=15_000)
        except PlaywrightTimeoutError:
            raise RuntimeError(
                f"SSO page loaded but no input fields appeared. URL: {page.url}"
            )

        # Fill the first visible text input (the "One Healthcare ID or Email Address" field)
        username_field = page.locator("input:visible").first
        username_field.fill(username)

        page.locator(
            "button:has-text('Continue'), button[type='submit'], input[type='submit']"
        ).first.click(timeout=10_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(1_500)

        # ── Password step ──────────────────────────────────────────────────────
        try:
            page.wait_for_selector("input[type='password']", state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            raise RuntimeError(
                f"Password field did not appear after submitting username. URL: {page.url}"
            )

        page.locator("input[type='password']").first.fill(password)

        page.locator(
            "button:has-text('Continue'), button[type='submit'], input[type='submit']"
        ).first.click(timeout=10_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
        page.wait_for_timeout(1_500)

        # ── RBA options page (#/rba/options) ───────────────────────────────────
        # OneHealthcareID may show a "how do you want your code?" screen.
        # Click the first available send-code option (email or phone), then
        # fall through to the code-entry step below.
        if "#/rba/options" in page.url or "/rba/" in page.url:
            # Try clicking the first radio/button option (email, phone, etc.)
            rba_option_selectors = [
                "input[type='radio']",
                "button:has-text('Email')",
                "button:has-text('Phone')",
                "button:has-text('Text')",
                "li button",
                "[class*='option'] button",
                "[class*='option'] input",
            ]
            for sel in rba_option_selectors:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=3_000)
                    loc.click()
                    break
                except PlaywrightTimeoutError:
                    continue

            # Click the Continue/Send button to dispatch the code
            try:
                page.locator(
                    "button:has-text('Continue'), button:has-text('Send'), "
                    "button[type='submit'], input[type='submit']"
                ).first.click(timeout=8_000)
                try:
                    page.wait_for_load_state("load", timeout=10_000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1_500)
            except PlaywrightTimeoutError:
                pass

        # ── Code entry step (#/rba/challenge or similar) ───────────────────────
        # Covers both traditional MFA and RBA challenge pages.
        if "onehealthcareid.com" in page.url and "uhcjarvis.com" not in page.url:
            mfa_selectors = [
                "input[type='text']",
                "input[name='otp']",
                "input[name='code']",
                "input[name='verificationCode']",
                "input[placeholder*='code' i]",
                "input[aria-label*='code' i]",
                "input[maxlength='6']",
                "input[maxlength='8']",
            ]
            mfa_field = None
            for sel in mfa_selectors:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=5_000)
                    mfa_field = loc
                    break
                except PlaywrightTimeoutError:
                    continue

            if mfa_field:
                code = mfa_fn()  # blocks until user submits via /setup/mfa
                mfa_field.fill(str(code))
                page.locator(
                    "button:has-text('Continue'), button:has-text('Verify'), "
                    "button:has-text('Submit'), button[type='submit']"
                ).first.click(timeout=10_000)
                # Use load not networkidle — some pages poll indefinitely
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2_000)

        # ── Handle "Trust this device?" / "Keep me signed in?" prompts ────────
        trust_selectors = [
            "button:has-text('Trust')",
            "button:has-text('Yes, trust')",
            "button:has-text('Keep me signed in')",
            "button:has-text('Remember this device')",
            "button:has-text('Yes')",
        ]
        for sel in trust_selectors:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=3_000)
                loc.click()
                page.wait_for_timeout(2_000)
                break
            except PlaywrightTimeoutError:
                continue

        # ── Confirm we're back on Jarvis ───────────────────────────────────────
        try:
            page.wait_for_url("*uhcjarvis.com*", timeout=20_000)
        except PlaywrightTimeoutError:
            pass

        if "onehealthcareid.com" in page.url:
            context.close()
            browser.close()
            raise RuntimeError(
                f"Still on SSO after verification ({page.url}). "
                "Visit /setup/screenshot to see what the browser is stuck on."
            )

        context.storage_state(path=str(SESSION_FILE))
        b64 = base64.b64encode(SESSION_FILE.read_bytes()).decode()

        # Make the session available to run_export() immediately without a redeploy
        os.environ["JARVIS_SESSION_B64"] = b64

        context.close()
        browser.close()

    return b64

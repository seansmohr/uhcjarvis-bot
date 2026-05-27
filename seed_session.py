#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "python-dotenv"]
# ///
"""
Run this LOCALLY (not on Railway) to seed your Jarvis session.

  uv run seed_session.py

Opens a real browser window, logs you in, asks for your MFA code, then prints
a base64 string. Paste that string as JARVIS_SESSION_B64 in Railway.
Re-run this whenever Railway emails you that the session has expired.
"""

import base64
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

LOGIN_URL = "https://www.uhcjarvis.com"
SESSION_FILE = Path("./jarvis_session.json")
USERNAME = os.getenv("JARVIS_USERNAME")
PASSWORD = os.getenv("JARVIS_PASSWORD")


def bail(msg: str):
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def run():
    if not USERNAME or not PASSWORD:
        bail("Set JARVIS_USERNAME and JARVIS_PASSWORD in .env first.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

        try:
            page.locator(
                "input[name='username'], input[type='email'], #username"
            ).first.fill(USERNAME, timeout=10_000)
        except PlaywrightTimeoutError:
            bail("Could not find username field.")

        try:
            page.locator(
                "input[name='password'], input[type='password'], #password"
            ).first.fill(PASSWORD, timeout=10_000)
        except PlaywrightTimeoutError:
            bail("Could not find password field.")

        try:
            page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Sign In'), button:has-text('Log In')"
            ).first.click(timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            bail("Could not click Sign In.")

        # MFA
        mfa_selectors = [
            "input[name='otp']",
            "input[name='code']",
            "input[name='verificationCode']",
            "input[placeholder*='code' i]",
            "input[aria-label*='code' i]",
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
            print("\n[MFA] Check your email or phone for the verification code.")
            code = input("[MFA] Enter code: ").strip()
            if not code:
                bail("No MFA code entered.")
            mfa_field.fill(code)
            page.locator(
                "button[type='submit'], button:has-text('Verify'), "
                "button:has-text('Submit'), button:has-text('Continue')"
            ).first.click(timeout=10_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
        else:
            print("[MFA] No MFA prompt detected — continuing.")

        url = page.url.lower()
        if "login" in url or "signin" in url or "auth" in url:
            bail("Still on login page after submitting. Check your credentials.")

        context.storage_state(path=str(SESSION_FILE))
        print(f"\n[ok] Session saved to {SESSION_FILE}")

        b64 = base64.b64encode(SESSION_FILE.read_bytes()).decode()

        print("\n" + "=" * 60)
        print("Copy this value into Railway as JARVIS_SESSION_B64:")
        print("=" * 60)
        print(b64)
        print("=" * 60)
        print("\nDone. You can close the browser.\n")

        context.close()
        browser.close()


if __name__ == "__main__":
    run()

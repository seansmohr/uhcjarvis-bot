import datetime
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from flask import Response
from jarvis_export import (
    SessionExpiredError, JARVIS_SIGNIN_URL,
    _stealth_browser, _stealth_context,
    run_export, setup_session,
)

load_dotenv()

app = Flask(__name__)
TRIGGER_TOKEN = os.getenv("TRIGGER_TOKEN")

# ── Export state ──────────────────────────────────────────────────────────────
_export = {"running": False, "last_run": None, "last_result": None}


def _export_job():
    _export["running"] = True
    try:
        path = run_export()
        _export["last_result"] = {"ok": True, "file": path}
    except SessionExpiredError as e:
        _export["last_result"] = {"ok": False, "error": "session_expired", "detail": str(e)}
    except Exception as e:
        _export["last_result"] = {"ok": False, "error": str(e)}
    finally:
        _export["running"] = False
        _export["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"


# ── Setup state ───────────────────────────────────────────────────────────────
_setup = {"status": "idle", "b64": None, "error": None}
_mfa_event = threading.Event()
_mfa_code: str = None


def _setup_job():
    def mfa_fn():
        _setup["status"] = "waiting_for_mfa"
        _mfa_event.wait(timeout=300)  # 5-minute window to enter the code
        return _mfa_code

    _setup.update({"status": "logging_in", "b64": None, "error": None})
    try:
        b64 = setup_session(mfa_fn)
        _setup.update({"status": "complete", "b64": b64})
    except Exception as e:
        _setup.update({"status": "error", "error": str(e)})


# ── Export routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    session_ready = bool(os.getenv("JARVIS_SESSION_B64") or _setup.get("b64"))
    return jsonify({
        "service": "Jarvis Book of Business Exporter",
        "session_ready": session_ready,
        "export": _export,
        "hint": "POST /run?token=TOKEN to export  |  GET /setup to configure session",
    })


@app.route("/run", methods=["POST"])
def run():
    if TRIGGER_TOKEN and request.args.get("token") != TRIGGER_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    if _export["running"]:
        return jsonify({"status": "already_running"}), 409
    threading.Thread(target=_export_job, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/status")
def status():
    return jsonify(_export)


# ── Setup routes ──────────────────────────────────────────────────────────────

@app.route("/setup")
def setup_page():
    return SETUP_HTML, 200, {"Content-Type": "text/html"}


@app.route("/setup/start", methods=["POST"])
def setup_start():
    if _setup["status"] in ("logging_in", "waiting_for_mfa", "completing"):
        return jsonify({"error": "setup already in progress"}), 409
    _mfa_event.clear()
    threading.Thread(target=_setup_job, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/setup/mfa", methods=["POST"])
def setup_mfa():
    global _mfa_code
    code = (request.json or {}).get("code", "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400
    _mfa_code = code
    _mfa_event.set()
    return jsonify({"ok": True})


@app.route("/setup/status")
def setup_status():
    return jsonify(_setup)


@app.route("/setup/screenshot")
def setup_screenshot():
    """Navigate to the SSO login page and return a screenshot for debugging."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _stealth_browser(p)
        context = _stealth_context(browser)
        page = context.new_page()
        page.goto(JARVIS_SIGNIN_URL, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(2_000)
        try:
            page.locator(
                "button:has-text('Sign in with One Healthcare ID'), "
                "a:has-text('Sign in with One Healthcare ID')"
            ).first.click(timeout=8_000)
            page.wait_for_url("*onehealthcareid.com*", timeout=15_000)
            page.wait_for_load_state("networkidle", timeout=10_000)
            page.wait_for_selector("input", state="visible", timeout=10_000)
        except Exception:
            pass
        img = page.screenshot(full_page=True)
        context.close()
        browser.close()
    return Response(img, mimetype="image/png")


# ── Setup page HTML ───────────────────────────────────────────────────────────

SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Session Setup</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; color: #1a1a1a; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; padding: 24px; }
  .card { background: #fff; border-radius: 12px; padding: 40px;
          max-width: 520px; width: 100%; box-shadow: 0 2px 16px rgba(0,0,0,.08); }
  h1 { font-size: 1.25rem; font-weight: 600; margin-bottom: 8px; }
  .sub { color: #666; font-size: .9rem; margin-bottom: 32px; line-height: 1.5; }
  .step { display: none; }
  .step.active { display: block; }
  .status-dot { display: inline-block; width: 10px; height: 10px;
                border-radius: 50%; margin-right: 8px; background: #ccc; }
  .status-dot.spin { background: #f59e0b; animation: pulse 1s infinite; }
  .status-dot.ok   { background: #22c55e; }
  .status-dot.err  { background: #ef4444; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  label { display: block; font-size: .85rem; font-weight: 500;
          margin-bottom: 6px; color: #444; }
  input[type=text] { width: 100%; padding: 10px 14px; border: 1px solid #d1d5db;
                     border-radius: 8px; font-size: 1rem; outline: none;
                     transition: border-color .15s; }
  input[type=text]:focus { border-color: #3b82f6; }
  button { margin-top: 16px; width: 100%; padding: 11px;
           background: #1d4ed8; color: #fff; border: none;
           border-radius: 8px; font-size: 1rem; font-weight: 500;
           cursor: pointer; transition: background .15s; }
  button:hover { background: #1e40af; }
  button:disabled { background: #93c5fd; cursor: default; }
  .b64-box { margin-top: 16px; background: #f1f5f9; border-radius: 8px;
             padding: 14px; font-size: .75rem; font-family: monospace;
             word-break: break-all; max-height: 120px; overflow-y: auto;
             border: 1px solid #e2e8f0; }
  .copy-btn { margin-top: 10px; background: #16a34a; }
  .copy-btn:hover { background: #15803d; }
  .note { margin-top: 20px; font-size: .82rem; color: #6b7280;
          background: #fffbeb; border: 1px solid #fde68a;
          border-radius: 8px; padding: 12px 14px; line-height: 1.5; }
  .err-msg { margin-top: 14px; color: #dc2626; font-size: .9rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Jarvis Session Setup</h1>
  <p class="sub">This establishes your UHC Jarvis login session so the exporter can run without you being present.</p>

  <!-- Step: idle -->
  <div class="step active" id="step-idle">
    <button id="btn-start" onclick="startSetup()">Start Login</button>
  </div>

  <!-- Step: logging in -->
  <div class="step" id="step-logging-in">
    <span class="status-dot spin"></span>Logging in to Jarvis&hellip;
  </div>

  <!-- Step: waiting for MFA -->
  <div class="step" id="step-mfa">
    <span class="status-dot spin"></span>MFA code required
    <br><br>
    <label for="mfa-input">Enter the code from your email or phone:</label>
    <input type="text" id="mfa-input" placeholder="123456" autocomplete="one-time-code" inputmode="numeric">
    <button id="btn-mfa" onclick="submitMfa()">Submit Code</button>
  </div>

  <!-- Step: completing -->
  <div class="step" id="step-completing">
    <span class="status-dot spin"></span>Completing login&hellip;
  </div>

  <!-- Step: complete -->
  <div class="step" id="step-complete">
    <span class="status-dot ok"></span><strong>Session established!</strong>
    <p style="margin-top:12px;font-size:.9rem;color:#444;">
      Copy the value below and add it as <code>JARVIS_SESSION_B64</code> in Railway &rarr; Variables.
      Then redeploy — after that, exports will run without any setup step.
    </p>
    <div class="b64-box" id="b64-display"></div>
    <button class="copy-btn" onclick="copyB64()">Copy to clipboard</button>
    <div class="note">
      <strong>Note:</strong> The current session is already active in this deployment
      and will work until the container restarts. Adding it as a Railway variable
      makes it survive restarts and redeploys.
    </div>
  </div>

  <!-- Step: error -->
  <div class="step" id="step-error">
    <span class="status-dot err"></span><strong>Setup failed</strong>
    <p class="err-msg" id="err-msg"></p>
    <button onclick="resetSetup()" style="margin-top:16px;">Try again</button>
  </div>
</div>

<script>
let polling = null;

function showStep(name) {
  document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
  const el = document.getElementById('step-' + name);
  if (el) el.classList.add('active');
}

async function startSetup() {
  document.getElementById('btn-start').disabled = true;
  await fetch('/setup/start', { method: 'POST' });
  showStep('logging-in');
  startPolling();
}

async function submitMfa() {
  const code = document.getElementById('mfa-input').value.trim();
  if (!code) return;
  document.getElementById('btn-mfa').disabled = true;
  showStep('completing');
  await fetch('/setup/mfa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

function startPolling() {
  polling = setInterval(pollStatus, 2000);
}

async function pollStatus() {
  const res = await fetch('/setup/status');
  const data = await res.json();

  if (data.status === 'waiting_for_mfa') {
    clearInterval(polling);
    showStep('mfa');
    document.getElementById('mfa-input').focus();
  } else if (data.status === 'complete') {
    clearInterval(polling);
    document.getElementById('b64-display').textContent = data.b64;
    showStep('complete');
  } else if (data.status === 'error') {
    clearInterval(polling);
    document.getElementById('err-msg').textContent = data.error;
    showStep('error');
  } else if (data.status === 'completing' || data.status === 'logging_in') {
    // still working, keep polling
    if (!polling) startPolling();
  }
}

function copyB64() {
  const text = document.getElementById('b64-display').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy to clipboard', 2000);
  });
}

function resetSetup() {
  showStep('idle');
  document.getElementById('btn-start').disabled = false;
}

// On load, check if setup is already mid-flight
(async () => {
  const res = await fetch('/setup/status');
  const data = await res.json();
  if (data.status === 'waiting_for_mfa') { showStep('mfa'); document.getElementById('mfa-input').focus(); }
  else if (data.status === 'complete')   { document.getElementById('b64-display').textContent = data.b64; showStep('complete'); }
  else if (data.status === 'error')      { document.getElementById('err-msg').textContent = data.error; showStep('error'); }
  else if (['logging_in','completing'].includes(data.status)) { showStep('logging-in'); startPolling(); }
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

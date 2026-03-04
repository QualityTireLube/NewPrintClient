#!/usr/bin/env python3
"""
QL Print Client — Quality Tire Label Printer
=============================================
A lightweight Flask-based print client that:
  1. Polls the Firebase print queue for pending jobs
  2. Claims, decodes, and prints labels via macOS CUPS
  3. Provides a local web dashboard at http://localhost:7010

Designed for Brother QL-800 label printers on macOS.
"""

import os
import sys
import time
import json
import base64
import tempfile
import subprocess
import threading
import signal
import logging
from datetime import datetime

try:
    from flask import Flask, jsonify, request, render_template_string
except ImportError:
    print("ERROR: Flask not installed. Run: pip3 install flask")
    sys.exit(1)

try:
    import requests as http_requests
except ImportError:
    print("ERROR: requests not installed. Run: pip3 install requests")
    sys.exit(1)

# ─── Configuration ──────────────────────────────────────────────────

SERVER_URL = os.environ.get(
    "PRINT_SERVER_URL",
    "https://us-central1-qualityexpress-c19f2.cloudfunctions.net/printApi"
)
API_KEY = os.environ.get("PRINT_API_KEY", "ql-print-2024")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
DEFAULT_PRINTER = os.environ.get("DEFAULT_PRINTER", "")
CLIENT_ID = os.environ.get("CLIENT_ID", "ql-mac-client")
CLIENT_NAME = os.environ.get("CLIENT_NAME", "Quality Tire Mac")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "7010"))

# ─── Logging ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ql-print-client")

# ─── State ───────────────────────────────────────────────────────────

polling_active = False
poll_thread = None
heartbeat_thread = None
print_log = []       # Recent activity log entries
print_errors = []    # Recent errors
log_forward_buffer = []  # Logs queued for Firebase forwarding
job_stats = {"completed": 0, "failed": 0, "total_polled": 0}

MAX_LOG_ENTRIES = 200
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats

# ─── HTTP Helpers ────────────────────────────────────────────────────

HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json",
}


def api_get(path, params=None):
    resp = http_requests.get(
        f"{SERVER_URL}{path}", headers=HEADERS, params=params, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def api_post(path, data=None):
    resp = http_requests.post(
        f"{SERVER_URL}{path}", headers=HEADERS, json=data or {}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def api_put(path, data=None):
    resp = http_requests.put(
        f"{SERVER_URL}{path}", headers=HEADERS, json=data or {}, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def add_log(message, level="info", job_id=None, printer=None):
    """Append to in-memory log visible on the dashboard and queue for Firebase."""
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    print_log.append(entry)
    if len(print_log) > MAX_LOG_ENTRIES:
        del print_log[: len(print_log) - MAX_LOG_ENTRIES]

    if level == "error":
        print_errors.append(entry)
        if len(print_errors) > 50:
            del print_errors[:10]

    # Queue for Firebase forwarding
    log_forward_buffer.append({
        "level": level,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "jobId": job_id,
        "printer": printer,
    })

    log_fn = log.error if level == "error" else (log.warning if level == "warn" else log.info)
    log_fn(message)


# ─── CUPS Helpers ────────────────────────────────────────────────────


def get_cups_printers():
    """Return list of (name, status) tuples and the default printer name."""
    printers = []
    default = None
    try:
        result = subprocess.run(
            ["lpstat", "-p", "-d"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if line.startswith("printer "):
                parts = line.split()
                name = parts[1]
                status = "idle" if "idle" in line.lower() else (
                    "printing" if "printing" in line.lower() else "unknown"
                )
                printers.append((name, status))
            if "system default destination:" in line:
                default = line.split(":")[-1].strip()
    except Exception as e:
        add_log(f"Failed to detect CUPS printers: {e}", "error")
    return printers, default


# Paper sizes mirroring the dashboard's PAPER_SIZES constant
PAPER_SIZES = {
    "Brother-QL800":   {"width": 62, "height": 29},
    "Dymo-TwinTurbo":  {"width": 89, "height": 36},
    "29mmx90mm":       {"width": 90, "height": 29},
}


def print_pdf(pdf_path, printer_name, copies=1, paper_size=None):
    """
    Send a PDF to a CUPS printer via `lp`.
    Returns (success: bool, message: str).
    """
    cmd = ["lp"]
    if printer_name:
        cmd += ["-d", printer_name]
    if copies and copies > 1:
        cmd += ["-n", str(copies)]
    # Force PDF MIME type so CUPS doesn't fall back to text/plain
    cmd += ["-o", "document-format=application/pdf"]
    # Fit the PDF to the label media
    cmd += ["-o", "fit-to-page"]

    # Determine orientation from paper size — if width > height, it's landscape
    ps = PAPER_SIZES.get(paper_size or "")
    if ps and ps["width"] > ps["height"]:
        # orientation-requested=4 = landscape (90° CCW rotation)
        cmd += ["-o", "orientation-requested=4"]
        add_log(f"  Orientation: landscape (paper {paper_size}: {ps['width']}x{ps['height']}mm)")
    else:
        # Even if unknown, label stock is almost always landscape
        cmd += ["-o", "orientation-requested=4"]
        add_log(f"  Orientation: landscape (default for label stock)")

    cmd.append(pdf_path)

    add_log(f"  CUPS command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip() or f"lp exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "lp command timed out after 30s"
    except FileNotFoundError:
        return False, "lp command not found — is CUPS installed?"


# ─── Job Processing ─────────────────────────────────────────────────


def resolve_printer(job):
    """Pick the right CUPS printer for this job."""
    printer = job.get("printer") or job.get("printerName")
    if printer:
        return printer
    if DEFAULT_PRINTER:
        return DEFAULT_PRINTER
    _, default = get_cups_printers()
    if default:
        add_log(f"  No printer in job, using system default: {default}", "warn")
        return default
    return None


def process_job(job):
    """Claim → decode PDF → print → mark complete/failed."""
    job_id = job.get("id", "unknown")
    template = job.get("templateName") or job.get("formName") or "Unknown"
    copies = job.get("copies", 1)

    add_log(f"Processing job {job_id} — {template} (copies: {copies})", job_id=job_id)

    # 1. Claim
    try:
        claim = api_post(f"/api/print/jobs/{job_id}/claim", {"clientId": CLIENT_ID})
        add_log(f"  Claimed: {claim.get('message', 'ok')}")
    except http_requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 409:
            add_log(f"  Job {job_id} already claimed, skipping", "warn")
            return
        raise

    # 2. Resolve printer
    printer = resolve_printer(job)
    if not printer:
        msg = "No printer specified and no default configured"
        add_log(f"  {msg}", "error")
        api_post(f"/api/print/jobs/{job_id}/fail", {
            "clientId": CLIENT_ID,
            "errorMessage": msg,
            "shouldRetry": False,
        })
        job_stats["failed"] += 1
        return

    add_log(f"  Target printer: {printer}")

    # 3. Decode PDF
    pdf_data = job.get("pdfData")
    if not pdf_data:
        msg = "Job has no pdfData"
        add_log(f"  {msg}", "error")
        api_post(f"/api/print/jobs/{job_id}/fail", {
            "clientId": CLIENT_ID,
            "errorMessage": msg,
            "shouldRetry": False,
        })
        job_stats["failed"] += 1
        return

    tmp_path = None
    try:
        # Strip data URI prefix if present
        if pdf_data.startswith("data:"):
            pdf_data = pdf_data.split(",", 1)[1]

        pdf_bytes = base64.b64decode(pdf_data)

        # Validate it's actually a PDF
        if not pdf_bytes[:5] == b"%PDF-":
            add_log(
                f"  Decoded data is NOT a valid PDF! First 40 bytes: {pdf_bytes[:40]}",
                "error",
            )
            add_log(f"  pdfData starts with: {pdf_data[:60]}...", "error")
            api_post(f"/api/print/jobs/{job_id}/fail", {
                "clientId": CLIENT_ID,
                "errorMessage": "Decoded data is not a valid PDF (missing %PDF- header)",
                "shouldRetry": False,
            })
            job_stats["failed"] += 1
            return

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        add_log(f"  PDF decoded: {len(pdf_bytes)} bytes -> {tmp_path}")

        # 4. Print (pass paper size for correct orientation)
        paper_size = job.get("paperSize", "")
        success, message = print_pdf(tmp_path, printer, copies, paper_size)

        if success:
            add_log(f"  Printed successfully: {message}", job_id=job_id, printer=printer)
            api_post(f"/api/print/jobs/{job_id}/complete", {
                "clientId": CLIENT_ID,
                "printDetails": {
                    "printer": printer,
                    "copies": copies,
                    "cupsMessage": message,
                    "printedAt": datetime.now().isoformat(),
                },
            })
            job_stats["completed"] += 1
        else:
            add_log(f"  Print failed: {message}", "error", job_id=job_id, printer=printer)
            api_post(f"/api/print/jobs/{job_id}/fail", {
                "clientId": CLIENT_ID,
                "errorMessage": f"CUPS error: {message}",
                "shouldRetry": True,
            })
            job_stats["failed"] += 1

    except Exception as e:
        add_log(f"  Error processing job: {e}", "error")
        try:
            api_post(f"/api/print/jobs/{job_id}/fail", {
                "clientId": CLIENT_ID,
                "errorMessage": str(e),
                "shouldRetry": True,
            })
        except Exception:
            pass
        job_stats["failed"] += 1
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ─── Polling Loop ────────────────────────────────────────────────────


def poll_loop():
    """Background thread that polls for pending jobs."""
    global polling_active
    consecutive_errors = 0

    add_log("Polling started")

    while polling_active:
        try:
            data = api_get("/api/print/jobs/pending", {
                "limit": 5,
                "clientId": CLIENT_ID,
            })

            # Handle both {jobs: [...]} wrapper and plain array
            if isinstance(data, dict):
                jobs = data.get("jobs", [])
            elif isinstance(data, list):
                jobs = data
            else:
                jobs = []

            if jobs:
                job_stats["total_polled"] += len(jobs)
                add_log(f"Found {len(jobs)} pending job(s)")
                for job in jobs:
                    if not polling_active:
                        break
                    process_job(job)
                consecutive_errors = 0
            else:
                consecutive_errors = 0

        except http_requests.exceptions.ConnectionError:
            consecutive_errors += 1
            if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                add_log(f"Connection error (attempt {consecutive_errors})", "warn")
        except http_requests.exceptions.Timeout:
            consecutive_errors += 1
            add_log(f"Timeout (attempt {consecutive_errors})", "warn")
        except Exception as e:
            consecutive_errors += 1
            add_log(f"Poll error: {e}", "error")

        if consecutive_errors >= 10:
            add_log("Too many errors, backing off 30s", "error")
            time.sleep(30)
            consecutive_errors = 0
        elif polling_active:
            time.sleep(POLL_INTERVAL)

    add_log("Polling stopped")


# ─── Heartbeat & Log Forwarding ─────────────────────────────────────


def heartbeat_loop():
    """Background thread: sends heartbeat + forwards logs to Firebase."""
    global polling_active

    while polling_active:
        try:
            # Send heartbeat
            printers, _ = get_cups_printers()
            api_post("/api/print/clients/heartbeat", {
                "clientId": CLIENT_ID,
                "printerCount": len(printers),
                "stats": job_stats.copy(),
            })
        except Exception:
            pass  # Heartbeat failures are silent

        try:
            # Forward buffered logs
            if log_forward_buffer:
                batch = log_forward_buffer[:50]
                api_post("/api/print/logs", {
                    "clientId": CLIENT_ID,
                    "entries": batch,
                })
                del log_forward_buffer[:len(batch)]
        except Exception:
            pass  # Log forwarding failures are silent — logs stay in buffer

        try:
            # Update printer statuses
            printers, _ = get_cups_printers()
            if printers:
                statuses = [
                    {"systemName": name, "name": name, "status": "online" if st == "idle" else st}
                    for name, st in printers
                ]
                api_put("/api/print/printers/status", {
                    "clientId": CLIENT_ID,
                    "statuses": statuses,
                })
        except Exception:
            pass

        time.sleep(HEARTBEAT_INTERVAL)


# ─── Flask App ───────────────────────────────────────────────────────

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>QL Print Client</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 20px; }
  .container { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 24px; margin-bottom: 8px; }
  .subtitle { color: #94a3b8; margin-bottom: 20px; font-size: 14px; }

  .card { background: #1e293b; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; color: #60a5fa; }

  .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .stat { background: #0f172a; border-radius: 8px; padding: 14px; text-align: center; }
  .stat .value { font-size: 28px; font-weight: bold; }
  .stat .label { font-size: 12px; color: #94a3b8; margin-top: 4px; }

  .online { color: #4ade80; }
  .offline { color: #f87171; }
  .warn { color: #fbbf24; }

  .btn { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer;
         font-size: 14px; font-weight: 600; margin-right: 8px; margin-bottom: 8px; }
  .btn-green { background: #22c55e; color: #000; }
  .btn-red { background: #ef4444; color: #fff; }
  .btn-blue { background: #3b82f6; color: #fff; }
  .btn:hover { opacity: 0.85; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .printer-list { list-style: none; }
  .printer-list li { padding: 8px 12px; background: #0f172a; border-radius: 6px;
                     margin-bottom: 6px; display: flex; justify-content: space-between; }

  .log-box { background: #020617; border-radius: 8px; padding: 12px; max-height: 400px;
             overflow-y: auto; font-family: 'SF Mono', Menlo, monospace; font-size: 12px;
             line-height: 1.6; }
  .log-entry { padding: 2px 0; }
  .log-time { color: #475569; margin-right: 8px; }
  .log-info { color: #94a3b8; }
  .log-warn { color: #fbbf24; }
  .log-error { color: #f87171; }
</style>
</head>
<body>
<div class="container">
  <h1>🖨️ QL Print Client</h1>
  <p class="subtitle">Quality Tire Label Printer — Polling
    <span id="server-url">{{ server_url }}</span></p>

  <!-- Status -->
  <div class="card">
    <h2>Status</h2>
    <div class="status-grid">
      <div class="stat">
        <div class="value" id="poll-status">—</div>
        <div class="label">Polling</div>
      </div>
      <div class="stat">
        <div class="value" id="printer-count">—</div>
        <div class="label">Printers</div>
      </div>
      <div class="stat">
        <div class="value" id="completed-count">0</div>
        <div class="label">Completed</div>
      </div>
      <div class="stat">
        <div class="value" id="failed-count">0</div>
        <div class="label">Failed</div>
      </div>
    </div>
  </div>

  <!-- Controls -->
  <div class="card">
    <h2>Controls</h2>
    <button class="btn btn-green" id="btn-start" onclick="startPolling()">Start Polling</button>
    <button class="btn btn-red" id="btn-stop" onclick="stopPolling()" disabled>Stop Polling</button>
    <button class="btn btn-blue" onclick="refreshStatus()">Refresh</button>
    <button class="btn btn-blue" onclick="testConnection()">Test Server</button>
  </div>

  <!-- Printers -->
  <div class="card">
    <h2>CUPS Printers</h2>
    <ul class="printer-list" id="printer-list">
      <li>Loading...</li>
    </ul>
  </div>

  <!-- Log -->
  <div class="card">
    <h2>Activity Log</h2>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  return fetch(path, opts).then(r => r.json());
}

function refreshStatus() {
  api('GET', '/api/status').then(d => {
    document.getElementById('poll-status').textContent = d.polling ? 'Active' : 'Stopped';
    document.getElementById('poll-status').className = 'value ' + (d.polling ? 'online' : 'offline');
    document.getElementById('printer-count').textContent = d.printers.length;
    document.getElementById('completed-count').textContent = d.stats.completed;
    document.getElementById('failed-count').textContent = d.stats.failed;
    document.getElementById('btn-start').disabled = d.polling;
    document.getElementById('btn-stop').disabled = !d.polling;

    const list = document.getElementById('printer-list');
    if (d.printers.length === 0) {
      list.innerHTML = '<li>No printers detected</li>';
    } else {
      list.innerHTML = d.printers.map(p =>
        '<li><span>' + p.name + (p.is_default ? ' ⭐' : '') + '</span>' +
        '<span class="' + (p.status === 'idle' ? 'online' : 'warn') + '">' + p.status + '</span></li>'
      ).join('');
    }
  });

  api('GET', '/api/log').then(d => {
    const box = document.getElementById('log-box');
    box.innerHTML = d.log.slice(-100).map(e =>
      '<div class="log-entry"><span class="log-time">' + e.time + '</span>' +
      '<span class="log-' + e.level + '">' + e.message + '</span></div>'
    ).join('');
    box.scrollTop = box.scrollHeight;
  });
}

function startPolling() { api('POST', '/api/polling/start').then(refreshStatus); }
function stopPolling() { api('POST', '/api/polling/stop').then(refreshStatus); }
function testConnection() {
  api('GET', '/api/test-connection').then(d => {
    alert(d.success ? 'Server connection OK!' : 'Connection failed: ' + d.error);
  });
}

refreshStatus();
setInterval(refreshStatus, 5000);
</script>
</body>
</html>
"""


# ─── Flask Routes ────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, server_url=SERVER_URL)


@app.route("/api/status")
def status():
    printers, default = get_cups_printers()
    printer_list = [
        {"name": name, "status": st, "is_default": name == default}
        for name, st in printers
    ]
    return jsonify({
        "polling": polling_active,
        "server": SERVER_URL,
        "clientId": CLIENT_ID,
        "printers": printer_list,
        "stats": job_stats,
    })


@app.route("/api/log")
def get_log():
    return jsonify({"log": print_log[-100:]})


@app.route("/api/polling/start", methods=["POST"])
def start_polling():
    global polling_active, poll_thread, heartbeat_thread
    if polling_active:
        return jsonify({"message": "Already polling"})
    polling_active = True
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # Start heartbeat / log forwarding thread
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Register with server on start
    try:
        api_post("/api/print/clients/register", {
            "clientId": CLIENT_ID,
            "name": CLIENT_NAME,
            "description": f"QL Print Client v2.0 on {os.uname().nodename if hasattr(os, 'uname') else 'unknown'}",
        })
        add_log("Registered with server")
    except Exception as e:
        add_log(f"Registration failed: {e}", "warn")

    # Register printers (batch format expected by Firebase)
    try:
        printers, default = get_cups_printers()
        if printers:
            printer_list = [
                {
                    "name": name,
                    "systemName": name,
                    "systemPrinterName": name,
                    "type": "Label Printer",
                    "connectionType": "usb",
                    "status": "online" if st == "idle" else st,
                }
                for name, st in printers
            ]
            api_post("/api/print/printers", {
                "clientId": CLIENT_ID,
                "printers": printer_list,
            })
            add_log(f"Registered {len(printers)} printer(s) with server")
    except Exception as e:
        add_log(f"Printer registration failed: {e}", "warn")

    return jsonify({"message": "Polling started"})


@app.route("/api/polling/stop", methods=["POST"])
def stop_polling():
    global polling_active
    polling_active = False
    return jsonify({"message": "Polling stopped"})


@app.route("/api/test-connection")
def test_connection():
    try:
        result = api_get("/api/print/stats")
        return jsonify({"success": True, "stats": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ─── Startup ─────────────────────────────────────────────────────────

def main():
    log.info("=" * 50)
    log.info("QL Print Client v2.0")
    log.info("=" * 50)
    log.info(f"  Server:  {SERVER_URL}")
    log.info(f"  Client:  {CLIENT_ID}")
    log.info(f"  Port:    {FLASK_PORT}")

    printers, default = get_cups_printers()
    if printers:
        log.info(f"  Printers: {', '.join(n for n, s in printers)}")
        if default:
            log.info(f"  Default:  {default}")
    else:
        log.warning("  No CUPS printers detected!")

    # Auto-start polling
    global polling_active, poll_thread, heartbeat_thread
    polling_active = True
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    # Auto-start heartbeat / log forwarding
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Register with server
    try:
        api_post("/api/print/clients/register", {
            "clientId": CLIENT_ID,
            "name": CLIENT_NAME,
            "description": f"QL Print Client v2.0",
        })
        log.info("  Registered with server")
    except Exception as e:
        log.warning(f"  Registration failed: {e}")

    # Register printers (batch)
    if printers:
        try:
            printer_list = [
                {
                    "name": name,
                    "systemName": name,
                    "systemPrinterName": name,
                    "type": "Label Printer",
                    "connectionType": "usb",
                    "status": "online" if st == "idle" else st,
                }
                for name, st in printers
            ]
            api_post("/api/print/printers", {
                "clientId": CLIENT_ID,
                "printers": printer_list,
            })
            log.info(f"  Registered {len(printers)} printer(s)")
        except Exception as e:
            log.warning(f"  Printer registration failed: {e}")

    log.info(f"  Dashboard: http://localhost:{FLASK_PORT}")
    log.info("")

    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)


if __name__ == "__main__":
    main()

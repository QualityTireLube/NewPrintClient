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
import re
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

try:
    from pypdf import PdfReader as _PdfReader
except ImportError:
    _PdfReader = None

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
FALLBACK_POLL_INTERVAL = int(os.environ.get("FALLBACK_POLL_INTERVAL", "30"))

FIREBASE_DB_URL = "https://qualityexpress-c19f2-default-rtdb.firebaseio.com"
RTDB_SIGNAL_PATH = "printers/pendingSignal"

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
job_stats = {"completed": 0, "failed": 0, "total_polled": 0,
             "sse_wakes": 0, "fallback_wakes": 0}

MAX_LOG_ENTRIES = 200
HEARTBEAT_INTERVAL = 30  # seconds between heartbeats

# Threading event: set by the RTDB SSE listener to wake the poll loop instantly
wake_event = threading.Event()
rtdb_listener_active = False

# Cloudflare Tunnel — managed subprocess; URL auto-discovered from cloudflared stdout
cloudflare_tunnel_url: str = ""
_cloudflared_proc = None
# Incremented each time a new SSE listener is started; old threads check this
# against their own captured generation and exit when superseded, preventing
# the zombie-double-connection race that causes duplicate wake-up fires.
_sse_generation = 0

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
    "29mmx90mm":       {"width": 90, "height": 29},
    "Brother-QL800":   {"width": 62, "height": 29},
    "Dymo-TwinTurbo":  {"width": 89, "height": 36},
    "Dymo-30252":      {"width": 89, "height": 28},
    "Brother-DK2205":  {"width": 62, "height": 100},
    "Zebra-2x1":       {"width": 51, "height": 25},
}

# Map CUPS printer names to their default paper size key
PRINTER_DEFAULTS = {
    "Brother_QL_800":                 "29mmx90mm",
    "Brother_QL_800_2":               "29mmx90mm",
    "GODEX":                          "29mmx90mm",
    "Canon_TS3500_series":            None,
    "HP_LaserJet_400_M401n__B429A7_": None,
    "HP_LaserJet_Pro_M118_M119":      None,
}

# Brother QL-800 PPD PageSize codes (from `lpoptions -p Brother_QL_800_2 -l`)
# Key: (tape_width_mm, tape_length_mm) — always portrait (narrow x long)
BROTHER_QL_PAGESIZE = {
    (17, 54): "DC01",
    (17, 87): "DC02",
    (23, 23): "DC20",
    (29, 42): "DC08",
    (29, 90): "DC03",   # 1.1" x 3.5" standard address/tire label
    (38, 90): "DC04",
    (39, 48): "DC17",
    (52, 29): "DC24",
}


def get_pdf_dimensions_mm(pdf_path):
    """Return (width_mm, height_mm) from first page MediaBox, or (None, None)."""
    if _PdfReader is not None:
        try:
            page = _PdfReader(pdf_path).pages[0]
            return round(float(page.mediabox.width) / 72 * 25.4, 1), \
                   round(float(page.mediabox.height) / 72 * 25.4, 1)
        except Exception:
            pass
    # Fallback: regex scan for uncompressed PDFs
    try:
        raw = open(pdf_path, "rb").read()
        m = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", raw)
        if m:
            x0, y0, x1, y1 = (float(v) for v in m.groups())
            return round((x1 - x0) / 72 * 25.4, 1), round((y1 - y0) / 72 * 25.4, 1)
    except Exception:
        pass
    return None, None


def print_pdf(pdf_path, printer_name, copies=1, paper_size=None):
    """
    Send a PDF to a CUPS printer via `lp`.
    Returns (success: bool, message: str).
    """
    # Fall back to printer default if no paper size provided
    if not paper_size and printer_name:
        paper_size = PRINTER_DEFAULTS.get(printer_name)
        if paper_size:
            add_log(f"  Paper size from printer defaults: {paper_size}")

    cmd = ["lp"]
    if printer_name:
        cmd += ["-d", printer_name]
    if copies and copies > 1:
        cmd += ["-n", str(copies)]
    cmd += ["-o", "document-format=application/pdf"]
    cmd += ["-o", "fit-to-page"]

    is_brother_ql = printer_name and "Brother_QL" in printer_name

    # Read actual PDF dimensions so we can set media to match
    pdf_w, pdf_h = get_pdf_dimensions_mm(pdf_path)

    if is_brother_ql and pdf_w is not None:
        # The QL_Test dashboard generates landscape PDFs (e.g. 90×29mm) that
        # already match the physical label orientation.  Tell CUPS the media
        # is the same size as the PDF so fit-to-page prints it 1:1 with NO
        # rotation.  Do NOT use DC codes (portrait-defined) or
        # orientation-requested — both cause CUPS to rotate the content.
        cmd += ["-o", f"media=Custom.{pdf_w}x{pdf_h}mm"]
        add_log(f"  Brother QL media: Custom.{pdf_w}x{pdf_h}mm (matches PDF)")
        add_log(f"  Orientation: none (PDF matches tape; no rotation)")

    elif is_brother_ql:
        # Could not read PDF dims — fall back to PAPER_SIZES or printer default
        ps = PAPER_SIZES.get(paper_size or "")
        if ps:
            cmd += ["-o", f"media=Custom.{ps['width']}x{ps['height']}mm"]
            add_log(f"  Media: {paper_size} ({ps['width']}x{ps['height']}mm)")
        else:
            add_log(f"  Media: not set (could not read PDF dims or paper size)")
        add_log(f"  Orientation: auto (no PDF dims)")

    else:
        # Non-Brother printer: use Custom media from PAPER_SIZES lookup
        ps = PAPER_SIZES.get(paper_size or "")
        if ps:
            cmd += ["-o", f"media=Custom.{ps['width']}x{ps['height']}mm"]
            add_log(f"  Media: {paper_size} ({ps['width']}x{ps['height']}mm)")
        add_log(f"  Orientation: auto")

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
            # Permanent failures: printer doesn't exist in CUPS, bad PDF data, CUPS not installed.
            # These will never succeed on retry — mark as permanently failed.
            permanent_errors = (
                "no such file or directory",
                "does not exist",
                "unknown destination",
                "lp command not found",
                "not a valid pdf",
            )
            is_permanent = any(p in message.lower() for p in permanent_errors)
            api_post(f"/api/print/jobs/{job_id}/fail", {
                "clientId": CLIENT_ID,
                "errorMessage": f"CUPS error: {message}",
                "shouldRetry": not is_permanent,
            })
            job_stats["failed"] += 1

    except Exception as e:
        err_str = str(e)
        add_log(f"  Error processing job: {err_str}", "error")
        permanent_exc_errors = (
            "invalid base64",
            "not a valid pdf",
            "no such file or directory",
        )
        is_permanent_exc = any(p in err_str.lower() for p in permanent_exc_errors)
        try:
            api_post(f"/api/print/jobs/{job_id}/fail", {
                "clientId": CLIENT_ID,
                "errorMessage": err_str,
                "shouldRetry": not is_permanent_exc,
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


def start_rtdb_sse_listener():
    """
    Connect to Firebase RTDB REST streaming API (Server-Sent Events).
    When the dashboard creates a print job, the Cloud Function writes a
    signal to printers/pendingSignal. This SSE stream receives that push
    instantly and sets wake_event to unblock the poll loop.

    Uses only the `requests` library — no Firebase SDK, no credentials.
    Runs on a daemon thread. Automatically reconnects on failure.

    Generation counter (_sse_generation) prevents zombie threads from a
    previous polling session from creating a second live SSE connection
    and causing double wake-up fires.
    """
    global rtdb_listener_active, _sse_generation
    _sse_generation += 1
    my_gen = _sse_generation

    sse_url = f"{FIREBASE_DB_URL}/{RTDB_SIGNAL_PATH}.json"

    def sse_thread():
        global rtdb_listener_active
        reconnect_delay = 0.5
        # Tracks the last jobId we triggered on so we don't fire twice for
        # the same signal.  None on first-ever connect: the initial RTDB value
        # is used purely to establish a baseline (no wake fired for it).
        # Persists across reconnections so a job seen before a drop is NOT
        # re-fired after reconnect, but a NEW job written during the drop IS.
        last_seen_job_id = None

        while polling_active and _sse_generation == my_gen:
            try:
                add_log("SSE: connecting to Firebase RTDB...")
                resp = http_requests.get(
                    sse_url,
                    headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                    stream=True,
                    timeout=(10, 45),  # 45s read timeout; Firebase keep-alives arrive every ~30s
                )
                resp.raise_for_status()
                rtdb_listener_active = True
                reconnect_delay = 0.5  # reset on successful connect
                current_event_type = None

                for raw_line in resp.iter_lines():
                    if not polling_active or _sse_generation != my_gen:
                        break
                    if not raw_line:
                        continue

                    line = raw_line.decode("utf-8", errors="replace")

                    if line.startswith("event:"):
                        current_event_type = line[6:].strip()
                        continue

                    if not line.startswith("data:"):
                        continue

                    payload = line[5:].strip()
                    if not payload or payload == "null":
                        continue

                    # Only act on put/patch; ignore keep-alive, cancel, etc.
                    if current_event_type not in (None, "put", "patch"):
                        current_event_type = None
                        continue
                    current_event_type = None

                    try:
                        data = json.loads(payload)
                        if not isinstance(data, dict):
                            continue

                        signal_data = data.get("data")
                        if not signal_data or not isinstance(signal_data, dict):
                            continue

                        incoming_job_id = signal_data.get("jobId")

                        # Very first event ever: establish baseline silently.
                        # This prevents a spurious wake on the initial connection
                        # while still allowing a new job written during a
                        # reconnect to be detected (last_seen persists).
                        if last_seen_job_id is None:
                            last_seen_job_id = incoming_job_id or ""
                            continue

                        if incoming_job_id and incoming_job_id == last_seen_job_id:
                            continue

                        last_seen_job_id = incoming_job_id
                        add_log(">>> RTDB wake-up signal received — checking for jobs")
                        wake_event.set()
                    except json.JSONDecodeError:
                        pass

            except http_requests.exceptions.ConnectionError:
                rtdb_listener_active = False
                if polling_active and _sse_generation == my_gen:
                    add_log(f"SSE: connection lost, reconnecting in {reconnect_delay}s...", "warn")
            except http_requests.exceptions.Timeout:
                rtdb_listener_active = False
                if polling_active and _sse_generation == my_gen:
                    add_log(f"SSE: connect timeout, retrying in {reconnect_delay}s...", "warn")
            except Exception as e:
                rtdb_listener_active = False
                if polling_active and _sse_generation == my_gen:
                    add_log(f"SSE: error ({e}), reconnecting in {reconnect_delay}s...", "warn")

            if polling_active and _sse_generation == my_gen:
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 5)  # cap at 5s, not 30s

    thread = threading.Thread(target=sse_thread, daemon=True, name="rtdb-sse")
    thread.start()

    time.sleep(1.5)
    return rtdb_listener_active


def start_cloudflared_tunnel():
    """Spawn cloudflared as a managed subprocess and auto-discover the tunnel URL.
    A watchdog thread keeps it running and updates cloudflare_tunnel_url whenever
    cloudflared restarts with a new URL. Silently skips if cloudflared is not installed."""
    global _cloudflared_proc, cloudflare_tunnel_url

    if subprocess.run(["which", "cloudflared"],
                      capture_output=True).returncode != 0:
        add_log("cloudflared not found — tunnel disabled (run setup_cloudflared.command to install)", "warn")
        return

    metrics_port = FLASK_PORT + 1
    url_pattern  = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')

    def _watchdog():
        global _cloudflared_proc, cloudflare_tunnel_url
        while polling_active:
            add_log("Starting Cloudflare tunnel...")
            _cloudflared_proc = subprocess.Popen(
                ["cloudflared", "tunnel",
                 "--url",     f"http://localhost:{FLASK_PORT}",
                 "--metrics", f"localhost:{metrics_port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            cloudflare_tunnel_url = ""   # reset until we see the new URL
            for line in _cloudflared_proc.stdout:
                m = url_pattern.search(line)
                if m and not cloudflare_tunnel_url:
                    cloudflare_tunnel_url = m.group(0)
                    add_log(f"Cloudflare tunnel active: {cloudflare_tunnel_url}")
                if not polling_active:
                    _cloudflared_proc.terminate()
                    break
            _cloudflared_proc.wait()
            if polling_active:
                cloudflare_tunnel_url = ""
                add_log("Cloudflare tunnel exited — restarting in 5s...", "warn")
                time.sleep(5)

    threading.Thread(target=_watchdog, daemon=True, name="cloudflared-watchdog").start()


def poll_loop():
    """Background thread: waits for RTDB signal or fallback timeout, then checks for jobs."""
    global polling_active
    consecutive_errors = 0

    # Start the SSE listener for instant wake-ups
    has_rtdb = start_rtdb_sse_listener()
    if has_rtdb:
        add_log(f"Listening via Firebase RTDB (instant) + safety-net poll every {FALLBACK_POLL_INTERVAL}s")
    else:
        add_log(f"RTDB stream connecting — polling every {FALLBACK_POLL_INTERVAL}s until SSE is live")

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
                continue
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
            # Block until RTDB SSE signal wakes us, OR safety-net timeout
            woke_by_sse = wake_event.wait(timeout=FALLBACK_POLL_INTERVAL)
            wake_event.clear()
            if woke_by_sse:
                job_stats["sse_wakes"] += 1
            else:
                job_stats["fallback_wakes"] += 1
                # Only warn when there is genuinely no delivery mechanism active.
                # When the tunnel OR SSE is working this is a silent background check.
                if not rtdb_listener_active and not cloudflare_tunnel_url:
                    add_log("⚠ Fallback poll — no SSE or tunnel active, using polling only", "warn")

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
                "clientId":     CLIENT_ID,
                "printerCount": len(printers),
                "stats":        job_stats.copy(),
                "rtdbConnected": rtdb_listener_active,
                "sseWakes":     job_stats["sse_wakes"],
                "fallbackWakes": job_stats["fallback_wakes"],
                "tunnelUrl":    cloudflare_tunnel_url or None,
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
  <p class="subtitle">Quality Tire Label Printer — SSE + Fallback Poll —
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
        <div class="value" id="rtdb-status">—</div>
        <div class="label">RTDB Stream</div>
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
    document.getElementById('rtdb-status').textContent = d.rtdb_connected ? 'Connected' : 'Disconnected';
    document.getElementById('rtdb-status').className = 'value ' + (d.rtdb_connected ? 'online' : 'warn');
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
        "rtdb_connected": rtdb_listener_active,
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


@app.route("/api/print/jobs/receive", methods=["POST"])
def receive_job():
    """Direct-push endpoint called by the Cloud Function via Cloudflare Tunnel.
    Bypasses polling entirely — job is printed within milliseconds of being created."""
    key = request.headers.get("X-API-Key") or request.args.get("token")
    if API_KEY and key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    job = request.json
    if not job or not job.get("id"):
        return jsonify({"error": "Invalid job payload — 'id' required"}), 400
    threading.Thread(target=process_job, args=(job,), daemon=True).start()
    wake_event.set()  # counts as an SSE wake, not a fallback timeout
    return jsonify({"message": "received", "jobId": job["id"]}), 202


@app.route("/api/test-connection")
def test_connection():
    try:
        result = api_get("/api/print/stats")
        return jsonify({"success": True, "stats": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ─── Startup ─────────────────────────────────────────────────────────

def setup():
    """Start polling/heartbeat threads and register with server. Does NOT start Flask."""
    log.info("=" * 50)
    log.info("QL Print Client v2.1 (SSE + Fallback Poll)")
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

    global polling_active, poll_thread, heartbeat_thread
    polling_active = True
    poll_thread = threading.Thread(target=poll_loop, daemon=True)
    poll_thread.start()

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    try:
        api_post("/api/print/clients/register", {
            "clientId": CLIENT_ID,
            "name": CLIENT_NAME,
            "description": "QL Print Client v2.0",
        })
        log.info("  Registered with server")
    except Exception as e:
        log.warning(f"  Registration failed: {e}")

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

    start_cloudflared_tunnel()


def run_flask():
    """Start the Flask server (blocking)."""
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)


def main():
    setup()

    if "--no-browser" not in sys.argv:
        import webbrowser
        def _open_browser():
            time.sleep(2)
            webbrowser.open(f"http://localhost:{FLASK_PORT}")
        threading.Thread(target=_open_browser, daemon=True).start()

    run_flask()


if __name__ == "__main__":
    main()

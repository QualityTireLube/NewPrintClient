#!/usr/bin/env python3
"""
QL Print Client — Native App Launcher
Starts Flask in a background thread, then opens a native macOS window via pywebview.
Python-side socket polling navigates the window once Flask is ready — no JS cross-origin issues.
"""

import socket
import sys
import threading
import time

import webview

import print_client

LOADING_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>QL Print Client</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0f172a;
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100vh;
    gap: 20px;
  }
  .spinner {
    width: 48px; height: 48px;
    border: 4px solid #1e293b;
    border-top-color: #3b82f6;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  h1 { font-size: 22px; font-weight: 600; }
  p  { font-size: 14px; color: #64748b; }
</style>
</head>
<body>
  <div class="spinner"></div>
  <h1>QL Print Client</h1>
  <p>Starting print server…</p>
</body>
</html>
"""


def _start_flask():
    print_client.run_flask()


def _wait_then_navigate(window):
    """Poll via Python socket until Flask is up, then navigate the window."""
    port = print_client.FLASK_PORT
    for _ in range(120):   # up to 60 seconds
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    # Small extra delay so Flask finishes its own startup log lines
    time.sleep(0.3)
    window.load_url(f"http://localhost:{port}")


def main():
    print_client.setup()

    threading.Thread(target=_start_flask, daemon=True).start()

    window = webview.create_window(
        title="QL Print Client",
        html=LOADING_HTML,
        width=1100,
        height=750,
        min_size=(800, 600),
        resizable=True,
    )

    def on_gui_ready():
        # Called by pywebview after the GUI event loop is fully initialised.
        # Only safe to call load_url once we're inside this callback.
        threading.Thread(target=_wait_then_navigate, args=(window,), daemon=True).start()

    webview.start(func=on_gui_ready)

    print_client.polling_active = False
    sys.exit(0)


if __name__ == "__main__":
    main()

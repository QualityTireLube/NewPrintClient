# Quality Tire Print Client

A standalone print client that connects to the Quality Tire cloud print queue and automatically prints labels on your local printers. Built to work with the **QL Admin Dashboard** at [qualitytirelube.com/admin](https://qualitytirelube.com/admin/dashboard.html).

---

## How It Works

```
Admin Dashboard (web)  →  Firebase Cloud Queue  →  This Print Client  →  Local Printer
     (anywhere)              (cloud)               (shop Mac)            (Brother QL-800)
```

1. You (or a remote worker) create a label in the admin dashboard and click **Send to Print Client**
2. The label PDF gets stored in the Firebase cloud print queue
3. This print client polls the queue every 5 seconds
4. When a job is found, it downloads the PDF, sends it to the local printer, and reports success/failure

---

## Requirements

- **macOS** (10.15 Catalina or newer)
- **Python 3.8+** (pre-installed on modern Macs)
- A label printer installed and visible in System Settings → Printers (e.g., Brother QL-800)

---

## Install (Mac)

### Step 1: Download the code

Open **Terminal** (press `Cmd + Space`, type `Terminal`, hit Enter) and paste these commands one at a time:

```bash
cd ~
git clone https://github.com/KadeStanford/NewPrintClient.git
cd NewPrintClient
```

> **Don't have `git`?** If you get "command not found", macOS will prompt you to install Command Line Tools — click **Install** and try again. Or download the ZIP from https://github.com/KadeStanford/NewPrintClient → green **Code** button → **Download ZIP**, unzip it, then `cd ~/Downloads/NewPrintClient-main`.

### Step 2: Run the installer

```bash
chmod +x install.sh
./install.sh
```

The installer will:
1. Check that Python 3.8+ is installed
2. Create a virtual environment and install Flask + requests
3. Put a **Start Print Client** shortcut on your Desktop
4. Ask if you want auto-start on login (recommended — say **y**)
5. Ask if you want to start it right now

That's it. The print client is now running.

### Step 3: Verify it's working

1. Open your browser and go to **http://localhost:7010**
2. You should see the dashboard with:
   - **Polling: Active** (green)
   - Your printer(s) listed
3. Go to the admin dashboard, create a label, and click **Send to Print Client**
4. Watch it print

---

## Day-to-Day Usage

### Starting the client

Pick whichever method you prefer:

| Method | How |
|--------|-----|
| **Desktop shortcut** | Double-click **Start Print Client** on your Desktop |
| **Terminal** | Run `~/ql-print-client/start.sh` |
| **Auto-start** | If enabled during install, it starts when you log in — nothing to do |

### Dashboard

Once running, open **http://localhost:7010** in your browser to see:
- Connection status
- Detected printers and their status
- Print job statistics
- Live activity log

### Stopping

- **Terminal:** Press `Ctrl+C`
- **Auto-start:** Run `launchctl unload ~/Library/LaunchAgents/com.qualitytire.printclient.plist`

---

## Updating

When there's a new version:

```bash
cd ~/NewPrintClient
git pull
cp print_client.py ~/ql-print-client/
```

Then restart the print client.

---

## Configuration

All settings can be changed via environment variables or by editing the top of `print_client.py`.

| Setting | Default | Description |
|---------|---------|-------------|
| `PRINT_SERVER_URL` | Firebase URL | Print queue server |
| `PRINT_API_KEY` | `ql-print-2024` | API key |
| `CLIENT_ID` | `ql-mac-client` | Unique ID for this machine |
| `CLIENT_NAME` | `Quality Tire Mac` | Display name |
| `POLL_INTERVAL` | `5` | Seconds between queue checks |
| `DEFAULT_PRINTER` | (auto-detect) | Fallback CUPS printer name |
| `FLASK_PORT` | `7010` | Dashboard port |

Example with custom settings:
```bash
DEFAULT_PRINTER="Brother_QL_800" POLL_INTERVAL=3 ~/ql-print-client/start.sh
```

---

## Remote Worker Access

This system is cloud-based — no VPN or port forwarding needed:

- **Remote workers** create and queue labels from anywhere via the admin dashboard
- **This print client** at the shop pulls jobs from the cloud and prints them locally
- Monitor from any device on the same Wi-Fi: `http://[shop-mac-ip]:7010`

---

## Printer Setup (Brother QL-800)

The Brother QL-800 should already be set up on your Mac. To verify:

1. **System Settings → Printers & Scanners** — the QL-800 should be listed
2. If not, click **+** to add it (macOS auto-detects USB printers)
3. Verify in Terminal: `lpstat -p`

The print client auto-detects all installed printers and registers them with the server.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| **"No printers detected"** | Check the printer is on and connected. Run `lpstat -p` in Terminal. |
| **Dashboard shows "Stopped"** | Click **Start Polling** or restart the client. |
| **Jobs say "complete" but nothing prints** | Check the CUPS queue: `lpstat -o`. Also check the Activity Log on the dashboard. |
| **"Auth failed"** | Verify API key is `ql-print-2024` in the client config. |
| **Labels print wrong size** | Label size is controlled by the template in the admin dashboard, not the print client. |
| **Can't connect to server** | Check internet. Verify the server URL in config matches the Firebase URL. |

### Check CUPS printer status
```bash
lpstat -p -d
```

### View print queue
```bash
lpstat -o
```

### Clear stuck CUPS jobs
```bash
cancel -a
```

---

## Auto-Start Management

```bash
# Enable auto-start
launchctl load ~/Library/LaunchAgents/com.qualitytire.printclient.plist

# Disable auto-start
launchctl unload ~/Library/LaunchAgents/com.qualitytire.printclient.plist

# Check if running
launchctl list | grep qualitytire
```

---

## Uninstall

```bash
# Stop the service
launchctl unload ~/Library/LaunchAgents/com.qualitytire.printclient.plist 2>/dev/null

# Remove files
rm -rf ~/ql-print-client
rm -f ~/Desktop/Start\ Print\ Client.command
rm -f ~/Library/LaunchAgents/com.qualitytire.printclient.plist
```

---

## Files

```
NewPrintClient/
  print_client.py       ← Main application
  requirements.txt      ← Python dependencies (flask, requests)
  install.sh            ← macOS installer
  README.md             ← This file
```

After installation:
```
~/ql-print-client/
  print_client.py
  requirements.txt
  venv/                 ← Python virtual environment
  start.sh              ← Quick launcher
  config.json           ← (in ~/Library/Application Support/QLPrintClient/)
```

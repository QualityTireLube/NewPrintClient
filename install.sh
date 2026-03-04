#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Quality Tire Print Client — macOS Installer
# ═══════════════════════════════════════════════════════════════════
#
#  Usage:   chmod +x install.sh && ./install.sh
#
#  What this does:
#    1. Checks for Python 3.8+
#    2. Creates a virtual environment
#    3. Installs dependencies
#    4. Creates a launch script on the Desktop
#    5. Optionally sets up auto-start on login (launchd)
#

set -e

APP_NAME="QL Print Client"
INSTALL_DIR="$HOME/ql-print-client"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$INSTALL_DIR/venv"
PORT=7010

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Quality Tire Print Client — Installer      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ─── 1. Check Python ───
echo "→ Checking Python..."
if command -v python3 &>/dev/null; then
    PY=$(command -v python3)
    PY_VER=$($PY --version 2>&1 | awk '{print $2}')
    echo "  ✅ Found Python $PY_VER at $PY"
else
    echo "  ❌ Python 3 not found."
    echo ""
    echo "  Install Python first:"
    echo "    • macOS: brew install python3"
    echo "    • Or download from https://www.python.org/downloads/"
    echo ""
    exit 1
fi

# Check Python version >= 3.8
PY_MAJOR=$($PY -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PY -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo "  ❌ Python 3.8+ required (found $PY_VER)"
    exit 1
fi

# ─── 2. Create install directory ───
echo "→ Setting up install directory..."
mkdir -p "$INSTALL_DIR"

# Copy files
cp "$SCRIPT_DIR/print_client.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
echo "  ✅ Files copied to $INSTALL_DIR"

# ─── 3. Create virtual environment ───
echo "→ Creating virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    $PY -m venv "$VENV_DIR"
    echo "  ✅ Virtual environment created"
else
    echo "  ✅ Virtual environment already exists"
fi

# ─── 4. Install dependencies ───
echo "→ Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
echo "  ✅ Dependencies installed"

# ─── 5. Create launcher script on Desktop ───
echo "→ Creating launcher..."
LAUNCHER="$HOME/Desktop/Start Print Client.command"
cat > "$LAUNCHER" << 'LAUNCHER_SCRIPT'
#!/bin/bash
# Quality Tire Print Client Launcher
cd "$HOME/ql-print-client"
echo "Starting Quality Tire Print Client..."
echo "Dashboard will open in your browser."
echo "Keep this window open while printing."
echo ""
./venv/bin/python3 print_client.py --port 7010
LAUNCHER_SCRIPT
chmod +x "$LAUNCHER"
echo "  ✅ Launcher created on Desktop: Start Print Client.command"

# Also create a quick launch script in the install dir
LAUNCH_LOCAL="$INSTALL_DIR/start.sh"
cat > "$LAUNCH_LOCAL" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
./venv/bin/python3 print_client.py --port $PORT "\$@"
EOF
chmod +x "$LAUNCH_LOCAL"

# ─── 6. Offer auto-start ───
echo ""
echo "─── Auto-Start on Login (Optional) ───"
echo ""
read -p "  Start print client automatically on login? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/com.qualitytire.printclient.plist"
    mkdir -p "$PLIST_DIR"

    cat > "$PLIST_FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.qualitytire.printclient</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/python3</string>
        <string>${INSTALL_DIR}/print_client.py</string>
        <string>--port</string>
        <string>${PORT}</string>
        <string>--no-browser</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/stderr.log</string>
</dict>
</plist>
PLIST

    launchctl load "$PLIST_FILE" 2>/dev/null || true
    echo "  ✅ Auto-start enabled! Print client will start on login."
    echo "  To disable: launchctl unload $PLIST_FILE"
else
    echo "  ℹ️  Skipped. You can start manually from the Desktop shortcut."
fi

# ─── Done ───
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║          ✅ Installation Complete!            ║"
echo "╠══════════════════════════════════════════════╣"
echo "║                                              ║"
echo "║  To start:                                   ║"
echo "║    Double-click 'Start Print Client' on       ║"
echo "║    your Desktop                               ║"
echo "║                                              ║"
echo "║  Or run from terminal:                        ║"
echo "║    ~/ql-print-client/start.sh                 ║"
echo "║                                              ║"
echo "║  Dashboard: http://localhost:$PORT             ║"
echo "║                                              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Ask to start now
read -p "  Start the print client now? [Y/n] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo ""
    cd "$INSTALL_DIR"
    exec ./venv/bin/python3 print_client.py --port $PORT
fi

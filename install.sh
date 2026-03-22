#!/usr/bin/env bash
#
# PRODA MBS Checker - Debian/Ubuntu Install Script
# Installs all dependencies and configures the application for 1-click operation.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# After install, run with:
#   ./proda-mbs                           # interactive mode
#   ./proda-mbs --medicare X --irn Y --name Z   # single check
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
LAUNCHER="$SCRIPT_DIR/proda-mbs"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── Check we're on a Debian-based system ──────────────────────────────
if ! command -v apt-get &>/dev/null; then
    error "This script requires a Debian-based system (apt-get not found)."
fi

echo ""
echo "=============================================="
echo "  PRODA MBS Checker - Installation"
echo "=============================================="
echo ""

# ── 1. System packages ───────────────────────────────────────────────
info "Installing system dependencies..."
sudo apt-get update -qq

sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    curl \
    unzip \
    > /dev/null 2>&1

info "System packages installed."

# ── 2. Browser selection ─────────────────────────────────────────────
echo ""
echo "Select browser for automation:"
echo "  1) Firefox (recommended)"
echo "  2) Chrome"
echo ""
read -rp "Choice [1]: " BROWSER_CHOICE
BROWSER_CHOICE="${BROWSER_CHOICE:-1}"

if [ "$BROWSER_CHOICE" = "2" ]; then
    BROWSER_TYPE="chrome"
    info "Installing Google Chrome and ChromeDriver..."

    # Install Chrome if not present
    if ! command -v google-chrome &>/dev/null && ! command -v google-chrome-stable &>/dev/null; then
        wget -q -O /tmp/google-chrome.deb \
            "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
        sudo apt-get install -y -qq /tmp/google-chrome.deb > /dev/null 2>&1 || \
            sudo apt-get install -f -y -qq > /dev/null 2>&1
        rm -f /tmp/google-chrome.deb
    fi

    # ChromeDriver is managed automatically by Selenium 4.6+ (selenium-manager)
    info "Chrome installed. ChromeDriver will be auto-managed by Selenium."
else
    BROWSER_TYPE="firefox"
    info "Installing Firefox and GeckoDriver..."

    # Install Firefox if not present
    if ! command -v firefox &>/dev/null; then
        sudo apt-get install -y -qq firefox-esr > /dev/null 2>&1 || \
            sudo apt-get install -y -qq firefox > /dev/null 2>&1
    fi

    # Install geckodriver if not present
    if ! command -v geckodriver &>/dev/null; then
        GECKO_VERSION=$(curl -sL "https://api.github.com/repos/mozilla/geckodriver/releases/latest" \
            | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')

        if [ -z "$GECKO_VERSION" ]; then
            GECKO_VERSION="0.35.0"
            warn "Could not detect latest geckodriver version, using v${GECKO_VERSION}"
        fi

        ARCH=$(dpkg --print-architecture)
        case "$ARCH" in
            amd64) GECKO_ARCH="linux64" ;;
            arm64) GECKO_ARCH="linux-aarch64" ;;
            *)     error "Unsupported architecture: $ARCH" ;;
        esac

        wget -q -O /tmp/geckodriver.tar.gz \
            "https://github.com/mozilla/geckodriver/releases/download/v${GECKO_VERSION}/geckodriver-v${GECKO_VERSION}-${GECKO_ARCH}.tar.gz"
        sudo tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin/
        sudo chmod +x /usr/local/bin/geckodriver
        rm -f /tmp/geckodriver.tar.gz
        info "GeckoDriver v${GECKO_VERSION} installed to /usr/local/bin/"
    else
        info "GeckoDriver already installed: $(geckodriver --version | head -1)"
    fi
fi

# ── 3. Python virtual environment ────────────────────────────────────
info "Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

info "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r "$SCRIPT_DIR/requirements.txt" -q

info "Python dependencies installed."

# ── 4. Configuration file ────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    info "Creating config.yaml from template..."
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_FILE"

    # Set the selected browser type
    sed -i "s/type: \"firefox\"/type: \"${BROWSER_TYPE}\"/" "$CONFIG_FILE"

    echo ""
    echo "─────────────────────────────────────────────"
    echo "  Enter your PRODA credentials"
    echo "  (stored locally in config.yaml only)"
    echo "─────────────────────────────────────────────"
    echo ""

    read -rp "PRODA username: " PRODA_USER
    read -rsp "PRODA password: " PRODA_PASS
    echo ""

    if [ -n "$PRODA_USER" ] && [ -n "$PRODA_PASS" ]; then
        sed -i "s/username: \"\"/username: \"${PRODA_USER}\"/" "$CONFIG_FILE"
        # Escape special chars in password for sed
        ESCAPED_PASS=$(printf '%s\n' "$PRODA_PASS" | sed 's/[&/\]/\\&/g')
        sed -i "s/password: \"\"/password: \"${ESCAPED_PASS}\"/" "$CONFIG_FILE"
        info "Credentials saved to config.yaml"
    else
        warn "Credentials not provided. Edit config.yaml manually before running."
    fi

    # Lock down config file permissions (owner read/write only)
    chmod 600 "$CONFIG_FILE"
else
    info "config.yaml already exists, skipping credential setup."
    # Update browser type if different
    sed -i "s/type: \"firefox\"/type: \"${BROWSER_TYPE}\"/" "$CONFIG_FILE"
    sed -i "s/type: \"chrome\"/type: \"${BROWSER_TYPE}\"/" "$CONFIG_FILE"
fi

# ── 5. Google OAuth client_secret.json check ─────────────────────────
if [ ! -f "$SCRIPT_DIR/client_secret.json" ]; then
    warn "client_secret.json not found in $SCRIPT_DIR"
    echo ""
    echo "  To enable Gmail OTP retrieval, place your Google OAuth"
    echo "  client_secret.json file in: $SCRIPT_DIR/"
    echo ""
    echo "  Get it from: https://console.cloud.google.com/apis/credentials"
    echo "  Enable the Gmail API, create OAuth 2.0 Client ID (Desktop app),"
    echo "  and download the JSON file."
    echo ""
fi

# ── 6. Create launcher script ────────────────────────────────────────
info "Creating launcher script..."
cat > "$LAUNCHER" << 'LAUNCHER_EOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
python -m proda_mbs "$@"
LAUNCHER_EOF

chmod +x "$LAUNCHER"

# ── 7. Optional: create desktop shortcut ─────────────────────────────
DESKTOP_DIR="$HOME/Desktop"
if [ -d "$DESKTOP_DIR" ]; then
    read -rp "Create desktop shortcut? [y/N]: " CREATE_SHORTCUT
    if [[ "${CREATE_SHORTCUT,,}" == "y" ]]; then
        DESKTOP_FILE="$DESKTOP_DIR/proda-mbs.desktop"
        cat > "$DESKTOP_FILE" << DESKTOP_EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=PRODA MBS Checker
Comment=MBS Items Online Checker Automation
Exec=bash -c 'cd "$SCRIPT_DIR" && ./proda-mbs; read -p "Press Enter to close..."'
Icon=utilities-terminal
Terminal=true
Categories=Utility;
DESKTOP_EOF
        chmod +x "$DESKTOP_FILE"
        # Mark as trusted on GNOME
        if command -v gio &>/dev/null; then
            gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
        fi
        info "Desktop shortcut created."
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo -e "  ${GREEN}Installation complete!${NC}"
echo "=============================================="
echo ""
echo "  To run:"
echo "    cd $SCRIPT_DIR"
echo "    ./proda-mbs                                    # interactive mode"
echo "    ./proda-mbs --medicare X --irn Y --name Z      # single check"
echo "    ./proda-mbs --headless                         # headless browser"
echo "    ./proda-mbs --browser chrome                   # use Chrome"
echo ""
echo "  Config:  $CONFIG_FILE"
echo "  Logs:    console output"
echo ""
if [ ! -f "$SCRIPT_DIR/client_secret.json" ]; then
    echo -e "  ${YELLOW}⚠ Remember to add client_secret.json for Gmail OTP${NC}"
    echo ""
fi

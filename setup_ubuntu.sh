#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "=========================================="
echo "  FlowLocal - Setup Linux Mint / Ubuntu"
echo "=========================================="

# 1. Dipendenze di sistema per PyQt6, Audio (PortAudio) e FFmpeg
echo "[1/4] Verifica e installazione dipendenze di sistema (apt)..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y \
        libportaudio2 \
        libxcb-cursor0 \
        libxcb-xinerama0 \
        libxkbcommon-x11-0 \
        ffmpeg \
        curl
else
    echo "ATTENZIONE: apt-get non trovato. Assicurati di installare manualmente:"
    echo "  libportaudio2, libxcb-cursor0, libxcb-xinerama0, libxkbcommon-x11-0, ffmpeg, curl"
fi

# 2. Gestore Python uv & Ambiente Virtuale Isolato
echo "[2/4] Verifica installazione di uv..."
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv non trovato nel PATH. Installazione automatica in corso..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "ERRORE: impossibile trovare uv dopo l'installazione. Aggiungi \$HOME/.local/bin al PATH e riprova."
    exit 1
fi

echo "[3/4] Creazione ambiente virtuale isolato e sincronizzazione dipendenze (uv sync)..."
uv sync

# Pre-crea la directory dati utente su Linux (~/.local/share/FlowLocal)
USER_DATA_DIR="$HOME/.local/share/FlowLocal"
mkdir -p "$USER_DATA_DIR"

# 3. Voce nel Menu Applicazioni (.desktop)
echo "[4/4] Creazione launcher nel Menu Applicazioni..."
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

PYTHON_BIN="$APP_DIR/.venv/bin/python"
MAIN_SCRIPT="$APP_DIR/main.py"

if [ -f "$APP_DIR/mobile_pwa/icon-512.png" ]; then
    ICON_PATH="$APP_DIR/mobile_pwa/icon-512.png"
elif [ -f "$APP_DIR/assets/icon.png" ]; then
    ICON_PATH="$APP_DIR/assets/icon.png"
else
    ICON_PATH="$APP_DIR/app.ico"
fi

DESKTOP_FILE="$APPS_DIR/flowlocal.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=FlowLocal
Comment=Dettatura vocale locale, trascrizione acustica e studio AI
Exec=env QT_QPA_PLATFORM=xcb "$PYTHON_BIN" "$MAIN_SCRIPT"
Path=$APP_DIR
Icon=$ICON_PATH
Terminal=false
Categories=Office;Education;Utility;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo ""
echo "=========================================="
echo "  Installazione completata con successo!"
echo "=========================================="
echo "Directory app   : $APP_DIR"
echo "Dati utente     : $USER_DATA_DIR"
echo "Launcher menu   : $DESKTOP_FILE"
echo ""
echo "Puoi avviare FlowLocal dal Menu Applicazioni (categorie Office / Education / Utility)"
echo "oppure da terminale con:"
echo "  QT_QPA_PLATFORM=xcb \"$PYTHON_BIN\" \"$MAIN_SCRIPT\""
echo ""

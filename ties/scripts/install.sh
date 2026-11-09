#!/usr/bin/env bash
# Install ties on macOS or Linux and add a launcher.
#   cd ties && ./scripts/install.sh
# Re-running is safe: it updates the install and leaves your vault alone.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "ties needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

echo "Installing ties in $HERE/.venv …"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/python" -m pip install --quiet --upgrade pip
"$HERE/.venv/bin/python" -m pip install --quiet -e "$HERE[encryption,fast]"

TIES="$HERE/.venv/bin/ties"
VAULT="${TIES_VAULT:-$HOME/.ties/personal.vault}"

LAUNCH="#!/usr/bin/env bash
# Opens ties in your browser. Close this window to stop it.
exec \"$TIES\" --vault \"$VAULT\" serve --open"

case "$(uname -s)" in
  Darwin)
    mkdir -p "$HOME/Applications"
    TARGET="$HOME/Applications/ties.command"
    printf '%s\n' "$LAUNCH" > "$TARGET"
    chmod +x "$TARGET"
    echo "Launcher: $TARGET (double-click it, or drag it to the Dock)"
    ;;
  Linux)
    mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
    printf '%s\n' "$LAUNCH" > "$HOME/.local/bin/ties-app"
    chmod +x "$HOME/.local/bin/ties-app"
    cat > "$HOME/.local/share/applications/ties.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ties
Comment=Personal relationship graph
Exec=$HOME/.local/bin/ties-app
Terminal=true
Categories=Office;
DESKTOP
    echo "Launcher: 'ties' in your applications menu (or run ~/.local/bin/ties-app)"
    ;;
esac

if [ ! -e "$VAULT" ]; then
  echo
  echo "No vault yet. Creating an encrypted one at $VAULT (you'll choose a passphrase)."
  "$TIES" --vault "$VAULT" init --encrypt
fi
echo
echo "Done. Start ties with the launcher, or: $TIES --vault \"$VAULT\" serve --open"

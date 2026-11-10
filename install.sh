#!/usr/bin/env bash
# Install (or uninstall) the X11 → Wayland bridge as a systemd user service.
#
#   ./install.sh                     install deps, copy files, start service on :99
#   ./install.sh --listen :98        use a different proxy display
#   ./install.sh --with-videobridge  also build/install the customized Xwayland Video Bridge
#   ./install.sh --uninstall         stop and remove the service and installed files
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$HOME/.local/libexec/x11-wayland-bridge"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="x11-wayland-bridge.service"
LISTEN=":99"
WITH_VIDEOBRIDGE=0
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --listen) LISTEN="$2"; shift 2 ;;
        --with-videobridge) WITH_VIDEOBRIDGE=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

if [ "$UNINSTALL" = 1 ]; then
    say "Stopping and removing $UNIT"
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT"
    rm -rf "$PREFIX"
    systemctl --user daemon-reload
    say "Uninstalled."
    exit 0
fi

DESKTOP="${XDG_CURRENT_DESKTOP:-}"
case "${DESKTOP^^}" in
    *GNOME*) DESKTOP=gnome ;;
    *KDE*)   DESKTOP=kde ;;
    *) echo "Unsupported desktop '$DESKTOP' (need GNOME or KDE Wayland). Run this from your graphical session." >&2; exit 1 ;;
esac
say "Detected desktop: $DESKTOP"

# ---- dependencies --------------------------------------------------------
missing=()
need_py() { python3 -c "import $1" 2>/dev/null || missing+=("$2"); }
need_cmd() { command -v "$1" >/dev/null 2>&1 || missing+=("$2"); }

if command -v dnf >/dev/null; then
    PM="dnf"
    need_py PIL python3-pillow
    if [ "$DESKTOP" = kde ]; then
        need_py PyQt6.QtGui python3-pyqt6-base
        rpm -q kf6-kidletime >/dev/null 2>&1 || missing+=(kf6-kidletime)
        need_cmd spectacle spectacle
    else
        need_py gi python3-gobject
        need_cmd cc gcc
        rpm -q libX11-devel >/dev/null 2>&1 || missing+=(libX11-devel)
    fi
elif command -v pacman >/dev/null; then
    PM="pacman"
    need_py PIL python-pillow
    if [ "$DESKTOP" = kde ]; then
        need_py PyQt6.QtGui python-pyqt6
        pacman -Q kidletime >/dev/null 2>&1 || missing+=(kidletime)
        need_cmd spectacle spectacle
    else
        need_py gi python-gobject
        need_cmd cc base-devel
        pacman -Q libx11 >/dev/null 2>&1 || missing+=(libx11)
    fi
else
    PM=""
fi

if [ ${#missing[@]} -gt 0 ]; then
    if [ -z "$PM" ]; then
        echo "Missing dependencies and no supported package manager (dnf/pacman) found." >&2
        echo "Install Python 3 + Pillow, and PyQt6 + KIdleTime + Spectacle (KDE) or PyGObject + a C compiler + libX11 headers (GNOME)." >&2
        exit 1
    fi
    say "Installing dependencies: ${missing[*]}"
    if [ "$PM" = dnf ]; then
        sudo dnf install -y "${missing[@]}"
    else
        sudo pacman -S --needed --noconfirm "${missing[@]}"
    fi
else
    say "All dependencies already present"
fi

# ---- install files -------------------------------------------------------
say "Installing bridge to $PREFIX"
install -Dm644 "$SRC_DIR/bridge.py" "$PREFIX/bridge.py"
if command -v cc >/dev/null && [ -f /usr/include/X11/Xlib.h ]; then
    cc -O2 "$SRC_DIR/capture-selected.c" -lX11 -o "$PREFIX/capture-selected"
elif [ "$DESKTOP" = gnome ]; then
    echo "Cannot build capture-selected (needs cc and libX11 headers)." >&2; exit 1
fi

if [ "$WITH_VIDEOBRIDGE" = 1 ]; then
    say "Building customized Xwayland Video Bridge"
    cmake -S "$SRC_DIR/xwaylandvideobridge" -B "$SRC_DIR/xwaylandvideobridge/build" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$SRC_DIR/xwaylandvideobridge/build" --parallel
    install -Dm755 "$SRC_DIR/xwaylandvideobridge/build/bin/xwaylandvideobridge" "$HOME/.local/bin/xwaylandvideobridge"
    say "Installed ~/.local/bin/xwaylandvideobridge (enable 'Run on startup' from its tray menu)"
fi

# ---- systemd user service ------------------------------------------------
say "Writing $UNIT_DIR/$UNIT"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT" <<EOF
[Unit]
Description=X11 to Wayland screenshot and idle bridge
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $PREFIX/bridge.py --listen $LISTEN
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF

# Make sure the user manager has the graphical session's environment.
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
systemctl --user restart "$UNIT"
sleep 1

if systemctl --user is-active --quiet "$UNIT"; then
    say "Bridge running on display $LISTEN (starts automatically at login)."
    echo "    Launch apps through it with:  DISPLAY=$LISTEN your-x11-app"
    echo "    For muvm (close any running VM first):  DISPLAY=$LISTEN muvm --emu=fex /path/to/app"
    echo "    Logs:  journalctl --user -u $UNIT -f"
else
    echo "Service failed to start. Logs:" >&2
    journalctl --user -u "$UNIT" -n 30 --no-pager >&2
    exit 1
fi

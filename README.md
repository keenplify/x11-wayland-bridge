# X11 → KDE Wayland bridge

A local X11 protocol proxy for legacy applications running on a KDE Wayland desktop, including applications in `muvm`. It currently provides two APIs:

- Root-window `XGetImage` in `ZPixmap` format. Pixels come from a KDE Spectacle fullscreen capture.
- `MIT-SCREEN-SAVER` `QueryVersion` and `QueryInfo`. Idle milliseconds are derived from KDE's Wayland idle and resume notifications through KIdleTime.

Other X11 traffic is forwarded to the desktop's Xwayland server. Applications connect to the proxy display, for example `:99`, instead of the original display `:0`.

## Requirements

Linux with KDE Plasma Wayland, Xwayland, Spectacle, Python 3, Pillow, PyQt6, and KDE Frameworks 6 KIdleTime. On Fedora, these are typically provided by `spectacle`, `python3-pillow`, `python3-pyqt6-base`, and `kf6-kidletime`. The process must run in the logged-in graphical session with access to its Wayland socket and Xauthority file.

## Run

```sh
python3 bridge.py --listen :99 --upstream :0
DISPLAY=:99 your-x11-app
```

For `muvm`, set `DISPLAY=:99` on the host-side `muvm` command before starting its VM. An existing VM may need to be closed before its host display changes.

To use the included systemd unit, replace the `ExecStart` path and `--upstream` display with values for your account, copy it to `~/.config/systemd/user/x11-wayland-bridge.service`, then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now x11-wayland-bridge.service
```

## Scope and limitations

The screenshot path supports the first X screen, 32-bit little-endian pixels with standard RGB masks, and root-window `ZPixmap` requests. Capture can trigger KDE's screenshot permission prompt. A failed capture passes through Xwayland's original response.

The idle path implements only `QueryVersion` and `QueryInfo`, which are the calls needed by common `libXss` clients. KIdleTime's Wayland backend exposes idle **events**, not a direct millisecond counter, so the bridge starts counting after a one-second idle event and resets on resumed input. Values within that first second are zero. It does not expose raw keyboard or mouse events, and activity percentages that require those events may still be unavailable to an application in `muvm`.

The socket is local and mode `0600`. Any X11 application deliberately connected to it can request a capture of the host desktop. Run only applications you trust.

## Check the APIs

`xdpyinfo -display :99 -queryExtensions` should allow normal X11 connections. `libXss`'s `XScreenSaverQueryExtension` and `XScreenSaverQueryInfo` should succeed on `:99`. The extension is answered by `QueryExtension`; it is not added to Xwayland's `ListExtensions` output.

## Development

This is an experimental proxy. It has been tested with a KDE Wayland desktop on Fedora Asahi and an x86-64 Hubstaff client through `muvm` and FEX. It is designed as an independent folder so it can be versioned and published separately.

## Xwayland Video Bridge tray startup option

The Python proxy above and KDE's Xwayland Video Bridge are separate applications.
The video bridge exposes portal screen sharing to X11 applications and has a
system tray icon. This repository also stores the
[`Run on startup` tray-menu patch](patches/xwaylandvideobridge-run-on-startup.patch)
for that native KDE application.

Right-click the video bridge icon and toggle **Run on startup**. The checkbox
reflects the effective autostart entry. Checking it enables startup at graphical
login; unchecking it disables startup without stopping the current bridge.
Changes persist in
`~/.config/autostart/org.kde.xwaylandvideobridge.desktop` (or the corresponding
`XDG_CONFIG_HOME` directory). Existing launch commands, including a systemd
launcher, are preserved. A disabled user entry also overrides the system-wide
entry. If no user entry exists, the patch creates one for the running executable
with `--autostart`. Failed writes revert the checkbox and show a tray message.
The checkbox is provided for native installations; Flatpak keeps its existing
Background portal behavior.

### Apply and build

This patch was built and tested against KDE Xwayland Video Bridge 0.5.3,
commit `1a972a2`. With this repository cloned next to the KDE source checkout:

```sh
git clone https://github.com/KDE/xwaylandvideobridge.git
cd xwaylandvideobridge
git checkout 1a972a2
git apply ../x11-wayland-bridge/patches/xwaylandvideobridge-run-on-startup.patch
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Building requires a C++20 compiler, CMake, Qt 6 development packages, KDE
Frameworks 6 development packages, KPipeWire, and the XCB development packages
listed in the KDE project's CMake configuration.

For the locally customized native bridge, install the binary at the path already
used by its launcher, then restart its service when a capture interruption is
acceptable:

```sh
install -Dm755 build/bin/xwaylandvideobridge ~/.local/bin/xwaylandvideobridge-persistent
systemctl --user restart xwaylandvideobridge-user.service
```

Validation: rebuilt the native bridge; verified startup enable/disable persistence,
preservation of the existing systemd launcher and other desktop-entry groups,
system-entry overrides, and write failures. Both checkbox states were also tested
through the running tray menu, with the original startup configuration restored.

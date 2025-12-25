Flask app that uses the Google Photos Picker API (no PublicAlbum) to let a user pick photos from a public/shared album (or any items they select in Google Photos), then plays them as a full‑screen screensaver (Photo Frame).

📌 Why this approach?
As of March/April 2025, Google removed/blocked shared-album methods in the old Library API and restricted listing/searching to app-created content. The supported way to get items from a user’s library (including shared/public albums via user selection) is the Picker API: create a session → open pickerUri → poll for completion → list selected items via photospicker.googleapis.com.

```
google-photos-screensaver/
|-- .env
├── app.py
├── gphotos-screensaver.service
├── kiosk.service
├── kiosk.sh
├── requirements.txt
├── selected_media.json
└── tokens.json
```
tested on RPi3 + Raspbian
```
Linux raspberrypi 6.12.47+rpt-rpi-v8 #1 SMP PREEMPT Debian 1:6.12.47-1+rpt1 (2025-09-16) aarch64 GNU/Linux
```

INSTALLATION:
```
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -f requirements.txt
python app.py
# Visit http://localhost:5000/screensaver
```
Or to setup it as systemd service, use the *.service scripts.

here are **ready-to-use systemd service files** and a **kiosk launcher script** tailored to your cleaned `app.py` and folder layout.

> They’re created under `google-photos-screensaver/`:
>
> *   `gphotos-screensaver.service` — runs the Flask app at boot
> *   `kiosk.service` — launches Chromium in kiosk mode pointing at `/screensaver`
> *   `kiosk.sh` — the script the kiosk service executes

***

## 1) `gphotos-screensaver.service`

```ini
[Unit]
Description=Google Photos Screensaver (Flask app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
Group=%i
WorkingDirectory=/home/%i/google-photos-screensaver
Environment=PYTHONUNBUFFERED=1
Environment=FLASK_ENV=production
# Optional .env file; uncomment if you use it
# EnvironmentFile=/home/%i/google-photos-screensaver/.env
ExecStart=/usr/bin/python3 /home/%i/google-photos-screensaver/app.py
Restart=always
RestartSec=3

# Resource limits (optional)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

**Notes**

*   This service is defined with a **template** style (`%i`) so you can run it as your user (e.g., `rosen`). See usage below.
*   `WorkingDirectory` matches your repo: `/home/<user>/google-photos-screensaver`.

***

## 2) `kiosk.service`

```ini
[Unit]
Description=Chromium Kiosk for Google Photos Screensaver
After=graphical.target gphotos-screensaver@%i.service
Requires=gphotos-screensaver@%i.service

[Service]
Type=simple
User=%i
Group=%i
WorkingDirectory=/home/%i/google-photos-screensaver
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/%i/.Xauthority
ExecStart=/home/%i/google-photos-screensaver/kiosk.sh
Restart=always
RestartSec=3

# Hardening (optional)
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=graphical.target
```

**Notes**

*   Starts **after** your app so the browser has something to display.
*   Assumes X is on `DISPLAY=:0` and your `.Xauthority` is in your home (typical Raspberry Pi desktop).

***

## 3) `kiosk.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_HOST="http://localhost:5000"
APP_PATH="/screensaver"
URL="${APP_HOST}${APP_PATH}"

# Wait for app port to be reachable (simple loop)
for i in {1..30}; do
  if nc -z localhost 5000 2>/dev/null; then break; fi
  sleep 1
done

# Chromium flags for kiosk mode
CHROMIUM="/usr/bin/chromium-browser"
if [[ ! -x "$CHROMIUM" ]]; then
  CHROMIUM="/usr/bin/chromium"
fi

exec "$CHROMIUM" \
  --noerrdialogs \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --kiosk "$URL" \
  --incognito \
  --overscroll-history-navigation=0 \
  --start-fullscreen \
  --autoplay-policy=no-user-gesture-required \
  --disable-pinch \
  --disable-gesture-typing \
  --disable-infobars \
  --hide-crash-restore-bubble \
  --enable-features=OverlayScrollbar \
  --new-window
```

**Notes**

*   Waits up to \~30s for `localhost:5000` to become reachable before launching Chromium.
*   If your platform uses `chromium` instead of `chromium-browser`, it falls back automatically.

***

## Install & enable (step-by-step)

> Replace `<user>` with your Linux username (e.g., `rosen`). On Raspberry Pi OS/Ubuntu:

```bash
# From your repo folder:
cd ~/google-photos-screensaver
chmod +x kiosk.sh

# Copy services as templated units
sudo cp gphotos-screensaver.service /etc/systemd/system/gphotos-screensaver@.service
sudo cp kiosk.service               /etc/systemd/system/kiosk@.service

# Reload unit files
sudo systemctl daemon-reload

# Enable at boot (runs as your user)
sudo systemctl enable gphotos-screensaver@<user>.service
sudo systemctl enable kiosk@<user>.service

# Start now
sudo systemctl start gphotos-screensaver@<user>.service
sudo systemctl start kiosk@<user>.service

# Check status/logs
systemctl status gphotos-screensaver@<user>.service
journalctl -u gphotos-screensaver@<user>.service -f
systemctl status kiosk@<user>.service
journalctl -u kiosk@<user>.service -f
```

***

## Optional tweaks

*   **Environment file**: If you use `.env` for `GOOGLE_CLIENT_ID`, etc., uncomment `EnvironmentFile=/home/%i/google-photos-screensaver/.env` in `gphotos-screensaver.service`.
*   **Headless setups**: If you use **Wayland** or **no desktop**, kiosk might need alternatives (e.g., `xinit` or `weston`) and different flags; happy to tailor to your stack.
*   **Autologin to desktop**: Ensure your Pi/host is set to auto-login into the graphical session so the kiosk service has a display.

***

a **nightly kiosk restart** via systemd timer and a helper script. These live in `google-photos-screensaver/`:

*   `kiosk-restart.service` — runs a one-shot script to restart Chromium/kiosk.
*   `kiosk-restart.timer` — triggers the restart **every day at 03:00**.
*   `restart_kiosk.sh` — restarts the `kiosk@<user>.service`, and as a fallback kills Chromium and relaunches `kiosk.sh`.

***

## Files (full contents)

### `kiosk-restart.service`

```ini
[Unit]
Description=Nightly restart of Chromium kiosk
After=network-online.target

[Service]
Type=oneshot
User=%i
Group=%i
WorkingDirectory=/home/%i/google-photos-screensaver
ExecStart=/home/%i/google-photos-screensaver/restart_kiosk.sh

[Install]
WantedBy=multi-user.target
```

### `kiosk-restart.timer`

```ini
[Unit]
Description=Nightly timer to restart Chromium kiosk

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
AccuracySec=1m
Unit=kiosk-restart@%i.service

[Install]
WantedBy=timers.target
```

### `restart_kiosk.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

USER_NAME="${1:-$USER}"
SERVICE_NAME="kiosk@${USER_NAME}.service"

# Option 1: systemd restart (preferred)
if systemctl --user >/dev/null 2>&1; then
  # If running in a user systemd context
  systemctl --user restart "${SERVICE_NAME}" || true
fi

# Option 2: system-wide systemd (when enabled as system unit)
sudo systemctl restart "${SERVICE_NAME}" || true

# Fallback: kill chromium and relaunch via kiosk.sh
pkill -x chromium || true
pkill -x chromium-browser || true
sleep 2

/home/${USER_NAME}/google-photos-screensaver/kiosk.sh &>/dev/null &
```

> `restart_kiosk.sh` accepts an optional username as argument; otherwise it uses `$USER`.

***

## Install & enable the timer

> Replace `<user>` with the account that runs your kiosk (e.g., `rosen`).

```bash
cd ~/google-photos-screensaver
chmod +x restart_kiosk.sh

# Install templated units (note the @.service/@.timer)
sudo cp kiosk-restart.service /etc/systemd/system/kiosk-restart@.service
sudo cp kiosk-restart.timer   /etc/systemd/system/kiosk-restart@.timer

# Reload units
sudo systemctl daemon-reload

# Enable and start the timer for your user
sudo systemctl enable kiosk-restart@<user>.timer
sudo systemctl start  kiosk-restart@<user>.timer

# (Optional) test the service immediately
sudo systemctl start kiosk-restart@<user>.service

# Check status & next run time
systemctl status kiosk-restart@<user>.timer
systemctl list-timers --all | grep kiosk-restart
journalctl -u kiosk-restart@<user>.service -f
```

**How it works**

*   The **timer** fires daily at **03:00** (`OnCalendar=*-*-* 03:00:00`) and runs the templated service: `kiosk-restart@<user>.service`.
*   The **service** executes `restart_kiosk.sh`, which tries:
    1.  Restart `kiosk@<user>.service` (either in user or system scope).
    2.  If that fails, it kills any Chromium processes and relaunches via your existing `kiosk.sh`.

***

## Optional adjustments

*   **Change time**: Edit `OnCalendar` (e.g., `Mon..Fri 02:30` for weekdays).  
    See `man systemd.time` for syntax.
*   **Remove `sudo`** in `restart_kiosk.sh`: If your kiosk service is a **user unit only** (`systemctl --user`), you can drop the system-wide restart path and `sudo`.
*   **Wayland / different display**: If you’re on Wayland or a different display, adjust `Environment=DISPLAY` and how you launch the browser in `kiosk.service`.

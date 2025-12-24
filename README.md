Here’s a complete, hardened app.py for a self‑contained Flask app that uses the Google Photos Picker API (no PublicAlbum) to let a user pick photos from a public/shared album (or any items they select in Google Photos), then plays them as a full‑screen screensaver (Photo Frame).

📌 Why this approach?
As of March/April 2025, Google removed/blocked shared-album methods in the old Library API and restricted listing/searching to app-created content. The supported way to get items from a user’s library (including shared/public albums via user selection) is the Picker API: create a session → open pickerUri → poll for completion → list selected items via photospicker.googleapis.com.

```
tree google-photos-screensaver/
google-photos-screensaver/
├── app.py
├── gphotos-screensaver.service
├── kiosk.service
├── kiosk.sh
├── requirements.txt
├── selected_media.json
├── templates
│   ├── pick.html
│   └── screensaver.html
└── tokens.json
```
tested on Raspbian

INSTALLATION:
```
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -f requirements.txt
python app.py
# Visit http://localhost:5000/screensaver
```
Or to setup it as systemd service, use the *.service scripts.

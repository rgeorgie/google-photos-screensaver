#!/usr/bin/env python3
"""
Google Photos Screensaver (Flask + Picker API) — Raspberry Pi (cleaned)

What’s included:
- OAuth + Google Photos Picker session (create, poll, list picked items).
- One‑time **download** of picked media to local cache (**/cache/photos/** by default).
- Screensaver plays **from local cache** 24/7; falls back to remote proxy when cache is empty.
- HEIC/HEIF → JPEG conversion on download (if `pillow-heif` available).
- YouTube music: single‑video loop enforced (`playlist=VIDEO_ID`), playlist restart on END, watchdog.

Removed/trimmed:
- Unused endpoints (`/api/picker/session`, compat poll), favicon and healthz.
- Unused YT_HIDE_VIDEO parameter.
"""

import os
import time
import json
import logging
from urllib.parse import urlencode
from datetime import datetime
import io
import shutil

import requests
from flask import (
    Flask, redirect, request, session, url_for,
    render_template_string, flash, jsonify, Response, abort, send_file
)
from dotenv import load_dotenv

# Optional HEIC/HEIF decoding via Pillow + pillow-heif
try:
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        HEIF_ENABLED = True
    except Exception:
        HEIF_ENABLED = False
except Exception:
    Image = None
    HEIF_ENABLED = False

load_dotenv()

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/auth/callback")
SECRET_KEY = os.getenv("SECRET_KEY", "dev")

SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"

PICKER_BASE = "https://photospicker.googleapis.com/v1"
SELECTION_STORE = os.getenv("SELECTION_STORE", "selected_media.json")

ADVANCE_SECONDS_DEFAULT = int(os.getenv("ADVANCE_SECONDS", "10"))
REFRESH_MINUTES_DEFAULT = int(os.getenv("REFRESH_MINUTES", "60"))  # baseUrl validity ~60 mins

# --- YouTube music config ---
YT_VIDEO_ID = os.getenv("YT_VIDEO_ID", "")
YT_PLAYLIST_ID = os.getenv("YT_PLAYLIST_ID", "")
YT_VOLUME = int(os.getenv("YT_VOLUME", "60"))

# --- Server-side token store (for kiosk localhost fetches) ---
TOKENS_STORE = os.getenv("TOKENS_STORE", "tokens.json")
DISABLE_SESSION_AUTH_FOR_LOCAL = os.getenv("DISABLE_SESSION_AUTH_FOR_LOCAL", "true").lower() == "true"

# --- Auto-renew buffer for Picker sessions ---
SESSION_RENEW_BUFFER_SEC = int(os.getenv("SESSION_RENEW_BUFFER_SEC", "60"))

# Force crop param to encourage JPEG derivatives
FORCE_CROP_PARAM = os.getenv("FORCE_CROP_PARAM", "true").lower() == "true"

# --- Local cache ---
CACHE_DIR = os.getenv("CACHE_DIR", "/cache/photos/")
CACHE_INDEX = os.path.join(CACHE_DIR, "cache_index.json")
DL_WIDTH = int(os.getenv("DL_WIDTH", "1280"))
DL_HEIGHT = int(os.getenv("DL_HEIGHT", "800"))

app = Flask(__name__)
app.secret_key = SECRET_KEY
logging.basicConfig(level=logging.INFO)

# -------------------------- utilities --------------------------
def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def read_cache_index():
    ensure_cache_dir()
    if not os.path.exists(CACHE_INDEX):
        return []
    try:
        with open(CACHE_INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        app.logger.exception("Failed to read cache_index.json")
        return []


def write_cache_index(items):
    ensure_cache_dir()
    try:
        with open(CACHE_INDEX, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        app.logger.exception("Failed to write cache_index.json")


def save_media_items(items: list) -> None:
    with open(SELECTION_STORE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_media_items() -> list:
    if not os.path.exists(SELECTION_STORE):
        return []
    with open(SELECTION_STORE, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_seconds(d, default=5.0):
    if isinstance(d, (int, float)): return float(d)
    if isinstance(d, str) and d.endswith("s"):
        try: return float(d[:-1])
        except: return default
    return default


def build_media_url(item: dict, kind: str, w: int = 800, h: int = 480) -> str:
    base = item.get("baseUrl") or ""
    mt = (item.get("mimeType") or "").lower()
    if not base:
        return ""
    if kind == "video" or mt.startswith("video/") or "motion" in mt:
        return base + "=dv"
    if FORCE_CROP_PARAM:
        return f"{base}=w{w}-h{h}-c"
    return f"{base}=w{w}-h{h}"

# -------- token persistence & refresh ----------
def _merge_tokens(new_tok: dict, old_tok: dict) -> dict:
    merged = dict(old_tok or {})
    for k in ("access_token", "refresh_token", "token_type", "expires_in"):
        v = new_tok.get(k)
        if v is not None:
            merged[k] = v
    merged["saved_at"] = int(time.time())
    return merged


def save_tokens(tok: dict) -> None:
    old = {}
    if os.path.exists(TOKENS_STORE):
        try:
            with open(TOKENS_STORE, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            app.logger.exception("Failed to read tokens.json during save")
    data = _merge_tokens(tok, old)
    try:
        with open(TOKENS_STORE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        app.logger.info("Persisted tokens.json (has_refresh=%s)", bool(data.get("refresh_token")))
    except Exception:
        app.logger.exception("Failed to persist tokens.json")


def load_tokens() -> dict:
    if not os.path.exists(TOKENS_STORE): return {}
    try:
        with open(TOKENS_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        app.logger.exception("Failed to read tokens.json")
        return {}


def refresh_access_token(refresh_token: str) -> str | None:
    if not refresh_token:
        return None
    data = {
        "client_id": CLIENT_ID or "",
        "client_secret": CLIENT_SECRET or "",
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=20)
        if r.status_code != 200:
            app.logger.error("Refresh token failed: %s %s", r.status_code, r.text)
            return None
        tok = r.json()
        save_tokens(tok)
        return tok.get("access_token")
    except Exception:
        app.logger.exception("Refresh token exception")
        return None


def get_server_access_token() -> str | None:
    t = load_tokens()
    rt = t.get("refresh_token")
    at = t.get("access_token")
    if rt:
        new_at = refresh_access_token(rt)
        if new_at:
            return new_at
    return at

# -------------------------- client session token helpers --------------------------
def get_client_access_token() -> str | None:
    at = session.get("access_token")
    rt = session.get("refresh_token")
    saved_at = session.get("token_saved_at") or 0
    expires_in = session.get("token_expires_in") or 0
    now = int(time.time())

    should_refresh = bool(rt) and (
        (not at) or (expires_in and saved_at and (saved_at + expires_in - 60) <= now)
    )

    if should_refresh:
        new_at = refresh_access_token(rt)
        if new_at:
            session["access_token"] = new_at
            t = load_tokens()
            session["token_expires_in"] = t.get("expires_in") or session.get("token_expires_in") or 0
            session["token_saved_at"] = t.get("saved_at") or now
            return new_at

    return session.get("access_token")

# -------------------------- HTTP wrappers with 401/403 retry --------------------------
def picker_get(url: str) -> requests.Response:
    at = get_client_access_token()
    if not at:
        raise requests.HTTPError("No access token")
    headers = {"Authorization": f"Bearer {at}"}
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code in (401, 403):
        new_at = refresh_access_token(session.get("refresh_token"))
        if new_at:
            session["access_token"] = new_at
            headers = {"Authorization": f"Bearer {new_at}"}
            r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r


def picker_post(url: str, payload: dict) -> requests.Response:
    at = get_client_access_token()
    if not at:
        raise requests.HTTPError("No access token")
    headers = {"Authorization": f"Bearer {at}"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code in (401, 403):
        new_at = refresh_access_token(session.get("refresh_token"))
        if new_at:
            session["access_token"] = new_at
            headers = {"Authorization": f"Bearer {new_at}"}
            r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    return r

# -------------------------- session auto-renew helpers --------------------------
def _iso_to_epoch(iso_ts: str) -> float:
    if not iso_ts: return 0.0
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def _is_expired_or_close(expire_iso: str, buffer_seconds: int = SESSION_RENEW_BUFFER_SEC) -> bool:
    if not expire_iso: return True
    now = time.time()
    exp = _iso_to_epoch(expire_iso)
    return exp <= (now + buffer_seconds)


def _ensure_session(picking_config: dict | None = None) -> dict:
    sid = session.get("picker_session_id")
    exp = session.get("picker_expire_time")
    if (not sid) or _is_expired_or_close(exp):
        url = f"{PICKER_BASE}/sessions"
        r = picker_post(url, picking_config or {})
        data = r.json()
        session["picker_session_id"] = data.get("id")
        session["picker_uri"] = data.get("pickerUri")
        session["picker_expire_time"] = data.get("expireTime")
        app.logger.info("Created/renewed session id=%s exp=%s", session["picker_session_id"], session["picker_expire_time"]) 
        return data
    return {"id": sid, "pickerUri": session.get("picker_uri"), "expireTime": exp}


@app.errorhandler(Exception)
def handle_any_error(e):
    app.logger.exception("Unhandled exception")
    return ("Internal Server Error", 500)

# -------------------------- templates --------------------------
PICK_TEMPLATE = """
<!doctype html>
<meta charset="utf-8" />
<title>Google Photos → Screensaver (Picker)</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem}
a.button{display:inline-block;padding:.6rem 1rem;border:1px solid #08c;border-radius:6px;text-decoration:none;color:#fff;background:#08c;margin:.25rem .25rem .25rem 0}
.flash{background:#fee;border:1px solid #f99;padding:.75rem;margin-bottom:1rem}
.box{border:1px solid #ddd;padding:1rem;border-radius:8px}
</style>
<h1>Google Photos → Screensaver</h1>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}
<div class="box">
  <p><strong>Step 1:</strong> <a class="button" href="{{ url_for('auth_start') }}">Authorize with Google</a></p>
  <p><strong>Step 2:</strong> <a class="button" href="{{ url_for('create_session') }}">Create Picker Session</a></p>
  <p><strong>Step 3:</strong> <a class="button" href="{{ url_for('status') }}">Status (auto‑polls)</a> → fetch → screensaver</p>
  <p><strong>Step 4:</strong> <a class="button" href="{{ url_for('screensaver') }}">Launch screensaver</a></p>
  <p><strong>Diagnostics:</strong> <a class="button" href="{{ url_for('diag') }}">Open diagnostics</a></p>
</div>
"""

STATUS_TEMPLATE = """
<!doctype html>
<meta charset="utf-8" />
<title>Picker Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5}
a.button{display:inline-block;padding:.6rem 1rem;border:1px solid #08c;border-radius:6px;text-decoration:none;color:#fff;background:#08c;margin:.25rem 0}
.muted{color:#555}
#debug{font-family:ui-monospace,monospace;color:#555;margin-top:1rem}
.flash{background:#fee;border:1px solid #f99;padding:.75rem;margin:.75rem 0}
</style>
<h1>Waiting for Google Photos selection…</h1>
{% if picker_uri %}
  <p><a class="button" href="{{ picker_uri }}" target="_blank" rel="noopener">Open Google Photos Picker (new tab)</a>
     <span class="muted">(allow pop‑ups if blocked)</span></p>
  <p><a class="button" href="{{ picker_uri }}">Open Picker in this tab</a></p>
{% else %}
  <p><em>No picker URI found; please <a href="{{ url_for('create_session') }}">create a new session</a>.</em></p>
{% endif %}
{% if session_id %}<p>Session ID: <code>{{ session_id }}</code></p>{% endif %}
<p class="muted">Select photos/videos → press <strong>Done</strong>. The app will download your selection to <code>{{ cache_dir }}</code>.</p>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}
<div id="debug"></div>
<script>
async function poll(){
  try{
    const r = await fetch('/api/poll');
    document.getElementById('debug').textContent='Polling… '+new Date().toLocaleTimeString();
    if(!r.ok) throw new Error('poll failed '+r.status);
    const j = await r.json();
    if (j.renewed) { location.reload(); return; }
    if(j.ready){ window.location='/fetch-selected'; return; }
    const interval = (j.interval && typeof j.interval==='number')? j.interval : 5.0;
    setTimeout(poll, interval*1000);
  }catch(e){
    document.getElementById('debug').textContent='Polling error: '+e;
    setTimeout(poll, 5000);
  }
}
poll();
</script>
"""

SCREENSAVER_TEMPLATE = """
<!doctype html>
<meta charset="utf-8"/>
<title>Photos Screensaver</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
html,body{height:100%;width:100%;margin:0;background:#000;overflow:hidden;cursor:none}
.stage{position:fixed;inset:0;display:grid;place-items:center}
img,video{width:100vw;height:100vh;object-fit:scale-down;background:#000}
.fade{animation:fade .6s ease}@keyframes fade{from{opacity:0}to{opacity:1}}
.empty{color:#ccc;font-family:system-ui,sans-serif}
#log{position:fixed;left:8px;bottom:8px;color:#888;font:12px ui-monospace,monospace;max-width:95vw;white-space:pre-wrap}
#yt-sound{position:fixed;left:-10000px;top:-10000px;width:300px;height:300px;opacity:0;pointer-events:none}
#yt-controls{position:fixed;left:12px;bottom:12px;display:flex;gap:8px;background:rgba(0,0,0,0.55);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:8px 10px;color:#eee;font:13px system-ui,sans-serif;z-index:10000;transition:opacity .25s}
#yt-controls button{border:none;padding:6px 8px;border-radius:6px;background:#1a1a1a;color:#fff;cursor:pointer}
#yt-controls .spacer{width:1px;height:22px;background:#555;opacity:.4}
#yt-controls input[type=range]{width:120px;accent-color:#08c}
body.idle #yt-controls{opacity:0;pointer-events:none}
</style>
<div class="stage"></div><div id="log"></div>
<div id="yt-sound"></div>
<div id="yt-controls">
  <button id="yt-prev" title="Previous (Shift+Left)">⏮</button>
  <button id="yt-play" title="Play/Pause (Space)">⏯</button>
  <button id="yt-next" title="Next (Shift+Right)">⏭</button>
  <span class="spacer"></span>
  <button id="yt-mute" title="Mute/Unmute (M)">🔈</button>
  <input id="yt-volume" type="range" min="0" max="100" step="1" title="Volume">
  <span class="spacer"></span>
  <span id="yt-label" class="label" title="Now playing">Now playing…</span>
</div>

<!-- YouTube IFrame API -->
<script src="https://www.youtube.com/iframe_api"></script>

<script>
document.addEventListener('DOMContentLoaded', async () => {
  const USE_LOCAL   = {{ use_local|tojson }};
  const LOCAL_ITEMS = {{ local_items|tojson }};   // [{path, kind, filename}]
  const REMOTE_ITEMS= {{ remote_items|tojson }};  // [{baseUrl,mimeType,filename}]
  const INTERVAL    = {{ interval_seconds }} * 1000;
  const REFRESH_MS  = {{ refresh_minutes }} * 60 * 1000;
  const stage = document.querySelector('.stage');
  const log = document.getElementById('log');
  function logMsg(m){ log.textContent = m; }

  let consecutiveErrors = 0;
  function bumpError(tag, url){
    consecutiveErrors++;
    logMsg(tag+' error: '+url+' (count='+consecutiveErrors+')');
    if (consecutiveErrors >= 3) location.reload();
  }
  function resetErrors(){ consecutiveErrors = 0; }

  function viewWH(){ return { w: Math.max(1, Math.round(window.innerWidth || 800)), h: Math.max(1, Math.round(window.innerHeight || 480))}; }

  function isVideoRemote(it){ const mt=(it.mimeType||'').toLowerCase(); return mt.startsWith('video/') || mt.includes('motion'); }
  function remoteUrlFor(it, kind){ const {w,h}=viewWH(); const idx=REMOTE_ITEMS.indexOf(it); const q=new URLSearchParams({kind,w:String(w),h:String(h)}); return '/content/'+idx+'?'+q.toString(); }

  let idx = 0;
  async function showLocal(i){
    const it = LOCAL_ITEMS[i]; if(!it) return;
    stage.innerHTML=''; let el;
    if (it.kind === 'video'){
      el = document.createElement('video'); el.src = '/local/'+i; el.autoplay=true; el.loop=true; el.muted=true; el.playsInline=true;
      el.addEventListener('error', () => bumpError('Video', el.src));
      el.addEventListener('loadeddata', () => { logMsg('Playing video '+(it.filename||'')); resetErrors(); });
    } else {
      el = document.createElement('img'); el.src = '/local/'+i; el.alt=it.filename||'';
      el.addEventListener('error', () => bumpError('Image', el.src));
      el.addEventListener('load', () => { logMsg('Showing image '+(it.filename||'')); resetErrors(); });
    }
    el.className='fade'; stage.appendChild(el);
  }

  async function showRemote(i){
    const it = REMOTE_ITEMS[i]; if(!it) return;
    stage.innerHTML=''; let el, kind = isVideoRemote(it) ? 'video' : 'image'; const url = remoteUrlFor(it, kind);
    const onError = async () => { const fb = remoteUrlFor(it,'image'); const img=document.createElement('img'); img.src=fb; img.alt=it.filename||''; img.addEventListener('error',()=>logMsg('Fallback image error: '+fb)); img.addEventListener('load',()=>logMsg('Fallback image loaded: '+(it.filename||''))); stage.innerHTML=''; stage.appendChild(img); };
    if (kind==='video'){
      el=document.createElement('video'); el.src=url; el.autoplay=true; el.loop=true; el.muted=true; el.playsInline=true;
      el.addEventListener('error', async ()=>{ logMsg('Video error: '+url); await onError(); });
      el.addEventListener('loadeddata', ()=> logMsg('Playing video '+(it.filename||'')) );
    } else {
      el=document.createElement('img'); el.src=url; el.alt=it.filename||'';
      el.addEventListener('error', async ()=>{ logMsg('Image error: '+url); await onError(); });
      el.addEventListener('load', ()=> logMsg('Showing image '+(it.filename||'')) );
    }
    el.className='fade'; stage.appendChild(el);
  }

  const items = USE_LOCAL ? LOCAL_ITEMS : REMOTE_ITEMS;
  if(!items || !items.length){
    stage.innerHTML='<div class="empty">No items found. Pick & download first.</div>';
    logMsg('No ITEMS'); return;
  }

  async function show(i){ if (USE_LOCAL) return showLocal(i); else return showRemote(i); }
  await show(idx);
  setInterval(async () => { idx=(idx+1)%items.length; await show(idx); }, INTERVAL);
  if(!USE_LOCAL && REFRESH_MS>0) setInterval(()=>{ logMsg('Refreshing to renew baseUrl…'); location.reload(); }, REFRESH_MS);
});
</script>

<!-- YouTube music player (robust looping) -->
<script>
  const YT_VIDEO_ID    = {{ yt_video_id|tojson }};
  const YT_PLAYLIST_ID = {{ yt_playlist_id|tojson }};
  const YT_VOLUME      = {{ yt_volume|tojson }};
  const $play=document.getElementById('yt-play'), $prev=document.getElementById('yt-prev'), $next=document.getElementById('yt-next'), $mute=document.getElementById('yt-mute'), $vol=document.getElementById('yt-volume');
  let ytPlayer=null; let wd=null; const WD_MS=10000;
  function armWD(){ clearTimeout(wd); wd=setTimeout(()=>{ try{ if(ytPlayer.getPlayerState()!==YT.PlayerState.PLAYING){ ytPlayer.playVideo(); } }catch{} }, WD_MS); }
  function disWD(){ clearTimeout(wd); wd=null; }
  function restartFromBeginning(){ try{ if (YT_PLAYLIST_ID && typeof ytPlayer.playVideoAt==='function'){ ytPlayer.playVideoAt(0); } else { ytPlayer.seekTo(0,true); ytPlayer.playVideo(); } }catch(e){ console.warn('YT restart failed:', e); } }
  window.onYouTubeIframeAPIReady = function(){
    const baseVars={ autoplay:1, controls:0, disablekb:1, modestbranding:1, rel:0, fs:0, playsinline:1, loop:1, origin:location.origin };
    if (YT_VIDEO_ID && !YT_PLAYLIST_ID){
      const playerVars={ ...baseVars, playlist: YT_VIDEO_ID }; // required for single-video loop
      ytPlayer=new YT.Player('yt-sound',{ width:200,height:200, videoId:YT_VIDEO_ID, playerVars, events:{ onReady:onReady, onStateChange:onState, onError:onErr } });
    } else if (YT_PLAYLIST_ID){
      const playerVars={ ...baseVars, listType:'playlist', list:YT_PLAYLIST_ID };
      ytPlayer=new YT.Player('yt-sound',{ width:200,height:200, playerVars, events:{ onReady:onReady, onStateChange:onState, onError:onErr } });
    }
  };
  function onReady(){ try{ ytPlayer.setVolume(Math.max(0,Math.min(100, Number(YT_VOLUME)||60))); ytPlayer.unMute(); ytPlayer.playVideo(); armWD(); $vol.value=ytPlayer.getVolume(); }catch{} }
  function onState(e){ const st=e.data; if(st===YT.PlayerState.PLAYING){ disWD(); armWD(); } else if(st===YT.PlayerState.ENDED){ restartFromBeginning(); armWD(); } else if(st===YT.PlayerState.BUFFERING||st===YT.PlayerState.UNSTARTED||st===YT.PlayerState.CUED){ armWD(); } }
  function onErr(){ try{ if (YT_PLAYLIST_ID) ytPlayer.nextVideo(); else restartFromBeginning(); }catch{} armWD(); }
  $play.addEventListener('click',()=>{ try{ const st=ytPlayer.getPlayerState(); if(st===YT.PlayerState.PLAYING) ytPlayer.pauseVideo(); else ytPlayer.playVideo(); }catch{} });
  $prev.addEventListener('click',()=>{ try{ ytPlayer.previousVideo(); armWD(); }catch{} });
  $next.addEventListener('click',()=>{ try{ ytPlayer.nextVideo(); armWD(); }catch{} });
  $mute.addEventListener('click',()=>{ try{ if(ytPlayer.isMuted()) ytPlayer.unMute(); else ytPlayer.mute(); }catch{} });
  $vol.addEventListener('input',()=>{ try{ ytPlayer.setVolume(Number($vol.value)); }catch{} });
</script>
"""

DIAG_TEMPLATE = """
<!doctype html>
<meta charset="utf-8" />
<title>Diagnostics</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{font-family:system-ui,sans-serif;margin:2rem;background:#111;color:#eee}
pre{background:#222;padding:1rem;border-radius:8px;overflow:auto}
.item{margin-bottom:1rem;border:1px solid #333;padding:1rem;border-radius:8px}
img,video{max-width:100%;height:auto;background:#000}
a.link{color:#7fc7ff}
</style>
<h1>Diagnostics</h1>
<h2>Local cache</h2>
{% for it in local_items %}
  <div class="item">
    <div><strong>{{ loop.index }}.</strong> {{ it.filename or '(no filename)' }} — {{ it.kind }}</div>
    <div>Local path: <code>{{ it.path }}</code></div>
    <div style="margin-top:.5rem">
      {% if it.kind == 'video' %}
        <video src="{{ url_for('local', index=loop.index0) }}" controls muted playsinline></video>
      {% else %}
        <img src="{{ url_for('local', index=loop.index0) }}" alt="{{ it.filename or '' }}" />
      {% endif %}
    </div>
  </div>
{% endfor %}

<h2>Proxied (remote)</h2>
{% for it in remote_items[:5] %}
  <div class="item">
    <div><strong>{{ loop.index }}.</strong> {{ it.filename or '(no filename)' }} — {{ it.mimeType }}</div>
    <div>Proxied image URL:
      <a class="link" href="{{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480" target="_blank" rel="noopener">
        {{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480
      </a>
    </div>
    <div>Proxied video URL:
      <a class="link" href="{{ url_for('content', index=loop.index0) }}?kind=video" target="_blank" rel="noopener">
        {{ url_for('content', index=loop.index0) }}?kind=video
      </a>
    </div>
    <div style="margin-top:.5rem">
      {% if it.mimeType and it.mimeType.lower().startswith('video') %}
        <video src="{{ url_for('content', index=loop.index0) }}?kind=video" controls muted playsinline></video>
      {% else %}
        <img src="{{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480" alt="{{ it.filename or '' }}" />
      {% endif %}
    </div>
  </div>
{% endfor %}
<pre>Local index: {{ local_items|tojson(indent=2) }}</pre>
<pre>Remote items: {{ remote_items[:5]|tojson(indent=2) }}</pre>
"""

# -------------------------- routes --------------------------
@app.route("/")
def home():
    return redirect(url_for("pick"))

@app.route("/pick")
def pick():
    return render_template_string(PICK_TEMPLATE)

# ---- OAuth start/callback ----
@app.route("/auth/start")
def auth_start():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": SCOPE,
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))

@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")
    if not code:
        flash("Authorization failed: missing code.")
        return redirect(url_for("pick"))

    data = {
        "code": code,
        "client_id": CLIENT_ID or "",
        "client_secret": CLIENT_SECRET or "",
        "redirect_uri": REDIRECT_URI or "",
        "grant_type": "authorization_code",
    }
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=20)
        if r.status_code != 200:
            app.logger.error("Token exchange failed: %s %s", r.status_code, r.text)
            flash("Authorization failed (token exchange). Check client/secret/redirect URI.")
            return redirect(url_for("pick"))
        tok = r.json()
    except Exception:
        app.logger.exception("Token exchange exception")
        flash("Authorization failed due to a network error.")
        return redirect(url_for("pick"))

    session["access_token"] = tok.get("access_token")
    session["refresh_token"] = tok.get("refresh_token")
    session["token_type"] = tok.get("token_type", "Bearer")
    session["token_expires_in"] = tok.get("expires_in") or 0
    session["token_saved_at"] = int(time.time())

    if not session["access_token"]:
        flash("Authorization failed: no access token returned.")
        return redirect(url_for("pick"))

    save_tokens(tok)
    return redirect(url_for("create_session"))

# ---- Picker session lifecycle ----
@app.route("/create-session", methods=["GET", "POST"])
def create_session():
    access_token = get_client_access_token()
    if not access_token:
        flash("Not authorized. Please start authorization.")
        return redirect(url_for("auth_start"))
    try:
        data = _ensure_session(picking_config={})
    except Exception:
        app.logger.exception("Create session exception")
        flash("Failed to create Picker session.")
        return redirect(url_for("pick"))
    session_id = data.get("id")
    picker_uri = data.get("pickerUri")
    if not (session_id and picker_uri):
        flash("Picker session created but missing data. Try again.")
        return redirect(url_for("pick"))
    session["picker_session_id"] = session_id
    session["picker_uri"] = picker_uri
    return redirect(url_for("status"))

@app.route("/status")
def status():
    picker_uri = session.get("picker_uri")
    if picker_uri:
        picker_uri = picker_uri.rstrip("/") + "/autoclose"
    return render_template_string(STATUS_TEMPLATE, picker_uri=picker_uri, session_id=session.get("picker_session_id"), cache_dir=CACHE_DIR)

@app.route("/api/poll")
def api_poll():
    access_token = get_client_access_token()
    session_id = session.get("picker_session_id")
    if not (access_token and session_id):
        return jsonify({"ready": False, "interval": 5.0, "error": "no_session"}), 200
    status_url = f"{PICKER_BASE}/sessions/{session_id}"
    renewed = False
    try:
        if _is_expired_or_close(session.get("picker_expire_time"), buffer_seconds=30):
            _ensure_session()
            session_id = session.get("picker_session_id")
            status_url = f"{PICKER_BASE}/sessions/{session_id}"
            renewed = True
        r = picker_get(status_url)
        info = r.json()
        if info.get("expireTime"):
            session["picker_expire_time"] = info.get("expireTime")
        if info.get("mediaItemsSet"):
            return jsonify({"ready": True, "interval": 0, "renewed": renewed}), 200
        poll_cfg = info.get("pollingConfig", {})
        interval = parse_seconds(poll_cfg.get("pollInterval"), default=5.0)
        return jsonify({"ready": False, "interval": interval, "renewed": renewed}), 200
    except Exception:
        app.logger.exception("Poll exception")
        return jsonify({"ready": False, "interval": 5.0, "error": "exception"}), 200

@app.route("/fetch-selected")
def fetch_selected():
    access_token = get_client_access_token()
    session_id = session.get("picker_session_id")
    if not (access_token and session_id):
        flash("No active Picker session. Create session first.")
        return redirect(url_for("create_session"))

    # List picked items
    items_url = f"{PICKER_BASE}/mediaItems"
    all_items, page_token = [], None
    try:
        while True:
            params = {"sessionId": session_id, "pageSize": 100}
            if page_token: params["pageToken"] = page_token
            url = items_url + "?" + urlencode(params)
            r = picker_get(url)
            data = r.json()
            all_items.extend(data.get("mediaItems", []) or [])
            page_token = data.get("nextPageToken")
            if not page_token: break
    except Exception:
        app.logger.exception("Fetch selected exception")
        flash("Network error fetching selected items.")
        return redirect(url_for("status"))

    simplified = []
    for m in all_items:
        mf = m.get("mediaFile") or {}
        base = mf.get("baseUrl") or m.get("baseUrl")
        mime = mf.get("mimeType", m.get("mimeType", ""))
        filename = mf.get("filename", m.get("filename", ""))
        if not base: continue
        simplified.append({"baseUrl": base, "mimeType": mime, "filename": filename})

    if len(simplified) == 0:
        flash("No items selected. Pick in Google Photos and press Done.")
        return redirect(url_for("status"))

    save_media_items(simplified)

    # Download to local cache
    ensure_cache_dir()
    local_index = []

    def ext_for_mime(m: str, kind: str) -> str:
        m = (m or "").lower()
        if kind == 'video':
            if 'mp4' in m: return 'mp4'
            if 'webm' in m: return 'webm'
            return 'mp4'
        if 'jpeg' in m or 'jpg' in m: return 'jpg'
        if 'png' in m: return 'png'
        if 'gif' in m: return 'gif'
        if 'webp' in m: return 'webp'
        if 'heic' in m or 'heif' in m: return 'heic'
        return 'jpg'

    def is_video_mime(m: str) -> bool:
        m = (m or "").lower()
        return m.startswith('video/') or ('motion' in m)

    def auth_fetch(url: str) -> requests.Response:
        at = get_client_access_token()
        headers = {"Authorization": f"Bearer {at}"}
        r = requests.get(url, headers=headers, timeout=60, stream=True)
        if r.status_code in (401,403):
            new_at = refresh_access_token(session.get("refresh_token"))
            if new_at:
                session["access_token"] = new_at
                headers = {"Authorization": f"Bearer {new_at}"}
                r = requests.get(url, headers=headers, timeout=60, stream=True)
        return r

    downloaded = 0
    for i, item in enumerate(simplified):
        base = item['baseUrl']
        mime = (item['mimeType'] or '').lower()
        fname = (item.get('filename') or f"item_{i}").strip().replace('/', '_')
        kind = 'video' if is_video_mime(mime) else 'image'
        try:
            if kind == 'video':
                url = base + "=dv"
                r = auth_fetch(url)
                if r.status_code != 200:
                    app.logger.error("Video download failed %s: %s", r.status_code, url)
                    continue
                ctype = (r.headers.get('Content-Type','') or '').lower()
                ext = ext_for_mime(ctype, 'video')
                local_path = os.path.join(CACHE_DIR, f"{i}_{fname}.{ext}")
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*64):
                        if chunk: f.write(chunk)
                local_index.append({"path": local_path, "kind": "video", "filename": item.get('filename')})
                downloaded += 1
            else:
                url = build_media_url(item, 'image', w=DL_WIDTH, h=DL_HEIGHT)
                r = auth_fetch(url)
                if r.status_code != 200:
                    app.logger.error("Image download failed %s: %s", r.status_code, url)
                    continue
                ctype = (r.headers.get('Content-Type','application/octet-stream') or '').lower()
                data = r.content
                if ('heic' in ctype or 'heif' in ctype) and HEIF_ENABLED and Image is not None:
                    try:
                        img = Image.open(io.BytesIO(data))
                        buf = io.BytesIO()
                        img.convert("RGB").save(buf, format="JPEG", quality=90)
                        data = buf.getvalue()
                        ext = 'jpg'
                    except Exception:
                        app.logger.exception("HEIC→JPEG conversion failed; saving raw")
                        ext = ext_for_mime(ctype, 'image')
                else:
                    ext = ext_for_mime(ctype, 'image')
                local_path = os.path.join(CACHE_DIR, f"{i}_{fname}.{ext}")
                with open(local_path, 'wb') as f:
                    f.write(data)
                local_index.append({"path": local_path, "kind": "image", "filename": item.get('filename')})
                downloaded += 1
        except Exception:
            app.logger.exception("Download error for item %d", i)

    write_cache_index(local_index)

    # Delete session (optional cleanup)
    try:
        del_url = f"{PICKER_BASE}/sessions/{session_id}"
        at = get_client_access_token()
        headers = {"Authorization": f"Bearer {at}"}
        dr = requests.delete(del_url, headers=headers, timeout=20)
        if dr.status_code in (401, 403):
            new_at = refresh_access_token(session.get("refresh_token"))
            if new_at:
                session["access_token"] = new_at
                headers = {"Authorization": f"Bearer {new_at}"}
                dr = requests.delete(del_url, headers=headers, timeout=20)
    except Exception:
        app.logger.exception("Session delete failed")

    # Clear session identifiers
    session.pop("picker_session_id", None)
    session.pop("picker_uri", None)
    session.pop("picker_expire_time", None)

    flash(f"Downloaded {downloaded} items to local cache.")
    return redirect(url_for("screensaver"))

# ---- Media proxy + HEIC→JPEG ----
@app.route("/content/<int:index>")
def content(index: int):
    items = load_media_items()
    if index < 0 or index >= len(items):
        abort(404)
    item = items[index]
    kind = request.args.get("kind", "image")
    try:
        w = int(request.args.get("w", "800"))
        h = int(request.args.get("h", "480"))
    except ValueError:
        w, h = 800, 480

    url = build_media_url(item, kind, w=w, h=h)
    if not url:
        abort(404)

    access_token = get_client_access_token()
    is_local = request.remote_addr in ("127.0.0.1", "::1")
    if (not access_token) and is_local and DISABLE_SESSION_AUTH_FOR_LOCAL:
        access_token = get_server_access_token()
    if not access_token:
        abort(401)

    def authorized_fetch(at: str) -> requests.Response:
        headers = {"Authorization": f"Bearer {at}"}
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code in (401, 403):
            new_at = None
            if is_local and DISABLE_SESSION_AUTH_FOR_LOCAL:
                t = load_tokens()
                new_at = refresh_access_token(t.get("refresh_token"))
            else:
                new_at = refresh_access_token(session.get("refresh_token"))
                if new_at:
                    session["access_token"] = new_at
            if new_at:
                headers = {"Authorization": f"Bearer {new_at}"}
                r = requests.get(url, headers=headers, timeout=30)
        return r

    try:
        r = authorized_fetch(access_token)
        if r.status_code != 200:
            app.logger.error("Proxy fetch failed (%s): %s", r.status_code, url)
            abort(r.status_code)

        ctype = r.headers.get("Content-Type", "application/octet-stream").lower()
        data = r.content

        if kind == "image" and ("heic" in ctype or "heif" in ctype):
            if HEIF_ENABLED and Image is not None:
                try:
                    img = Image.open(io.BytesIO(data))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="JPEG", quality=90)
                    buf.seek(0)
                    resp = Response(buf.getvalue(), status=200, mimetype="image/jpeg")
                    resp.headers["Cache-Control"] = "private, max-age=1800"
                    return resp
                except Exception:
                    app.logger.exception("HEIC→JPEG conversion failed; falling back to raw bytes")
            else:
                app.logger.warning("HEIC content but HEIF decoding not available; returning raw bytes")

        resp = Response(data, status=200, mimetype=r.headers.get("Content-Type", "application/octet-stream"))
        resp.headers["Cache-Control"] = "private, max-age=1800"
        return resp
    except Exception:
        app.logger.exception("Proxy fetch exception for %s", url)
        abort(502)

# ---- Serve local cached files ----
@app.route("/local/<int:index>")
def local(index: int):
    items = read_cache_index()
    if index < 0 or index >= len(items):
        abort(404)
    path = items[index].get('path')
    if not path or not os.path.exists(path):
        abort(404)
    ext = os.path.splitext(path)[1].lower()
    mime = 'application/octet-stream'
    if ext in ('.jpg','.jpeg'): mime='image/jpeg'
    elif ext == '.png': mime='image/png'
    elif ext == '.gif': mime='image/gif'
    elif ext == '.webp': mime='image/webp'
    elif ext == '.mp4': mime='video/mp4'
    elif ext == '.webm': mime='video/webm'
    return send_file(path, mimetype=mime, conditional=True)

@app.route("/cache/clear")
def cache_clear():
    try:
        if os.path.exists(CACHE_DIR): shutil.rmtree(CACHE_DIR)
        ensure_cache_dir(); write_cache_index([])
        flash("Local cache cleared.")
    except Exception:
        app.logger.exception("Failed to clear cache"); flash("Failed to clear cache.")
    return redirect(url_for("diag"))

@app.route("/screensaver")
def screensaver():
    local_items = read_cache_index()
    remote_items = load_media_items()
    use_local = len(local_items) > 0
    try: interval_seconds = int(request.args.get("interval", ADVANCE_SECONDS_DEFAULT))
    except ValueError: interval_seconds = ADVANCE_SECONDS_DEFAULT
    try: refresh_minutes = int(request.args.get("refresh", REFRESH_MINUTES_DEFAULT))
    except ValueError: refresh_minutes = REFRESH_MINUTES_DEFAULT

    if use_local:
        refresh_minutes = 0

    return render_template_string(
        SCREENSAVER_TEMPLATE,
        use_local=use_local,
        local_items=local_items,
        remote_items=remote_items,
        interval_seconds=interval_seconds,
        refresh_minutes=refresh_minutes,
        yt_video_id=YT_VIDEO_ID,
        yt_playlist_id=YT_PLAYLIST_ID,
        yt_volume=YT_VOLUME,
    )

@app.route("/diag")
def diag():
    local_items = read_cache_index()
    remote_items = load_media_items()
    return render_template_string(
        DIAG_TEMPLATE,
        local_items=local_items,
        remote_items=remote_items,
    )

@app.route("/auth/signout")
def auth_signout():
    session.clear(); flash("Signed out."); return redirect(url_for("pick"))

if __name__ == "__main__":
    ensure_cache_dir()
    app.run(host="0.0.0.0", port=5000, debug=False)

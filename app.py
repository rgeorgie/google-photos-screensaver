#!/usr/bin/env python3
"""
Google Photos Screensaver (Flask + Picker API) — 7" Raspberry Pi (800x480)
- Status page opens Google Photos Picker (autoclose) and auto-polls the session.
- Fetches picked items and saves them locally.
- Screensaver uses a media proxy (/content/<index>) so the browser doesn’t need to be logged into Google.
- Images: baseUrl += =w{width}-h{height}; Videos/Motion: baseUrl += =dv (per Google docs).
- Diagnostics page (/diag) embeds proxied media and shows clickable test links.
"""

import os
import time
import json
import logging
from urllib.parse import urlencode

import requests
from flask import (
    Flask, redirect, request, session, url_for,
    render_template_string, flash, jsonify, Response, abort
)
from dotenv import load_dotenv

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

# --- YouTube music config (set via environment) ---
YT_VIDEO_ID = os.getenv("YT_VIDEO_ID", "")           # e.g., "utbIKghScn8"
YT_PLAYLIST_ID = os.getenv("YT_PLAYLIST_ID", "")     # e.g., "RDutbIKghScn8"
YT_VOLUME = int(os.getenv("YT_VOLUME", "60"))        # 0..100
YT_HIDE_VIDEO = os.getenv("YT_HIDE_VIDEO", "true").lower() == "true"

# --- Server-side token store (for kiosk localhost fetches) ---
TOKENS_STORE = os.getenv("TOKENS_STORE", "tokens.json")
DISABLE_SESSION_AUTH_FOR_LOCAL = os.getenv("DISABLE_SESSION_AUTH_FOR_LOCAL", "true").lower() == "true"

app = Flask(__name__)
app.secret_key = SECRET_KEY
logging.basicConfig(level=logging.INFO)


# -------------------------- utilities --------------------------
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
    """
    Build a Google Photos content URL from baseUrl + parameters.
    kind: 'image' or 'video'
    """
    base = item.get("baseUrl") or ""
    mt = (item.get("mimeType") or "").lower()
    if not base:
        return ""
    if kind == "video" or mt.startswith("video/") or "motion" in mt:
        return base + "=dv"  # video stream via '=dv' (not compatible with w/h)
    return f"{base}=w{w}-h{h}"  # image bytes via width/height params

# -------- token persistence & refresh (server-side) ----------
def _merge_tokens(new_tok: dict, old_tok: dict) -> dict:
    merged = dict(old_tok or {})
    for k in ("access_token", "refresh_token", "token_type", "expires_in"):
        v = new_tok.get(k)
        if v:
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
            app.logger.exception("Failed to read existing tokens.json during save")
    data = _merge_tokens(tok, old)
    try:
        with open(TOKENS_STORE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        app.logger.info("Persisted tokens.json (has_refresh=%s)", bool(data.get("refresh_token")))
    except Exception:
        app.logger.exception("Failed to persist tokens.json")

def load_tokens() -> dict:
    if not os.path.exists(TOKENS_STORE):
        return {}
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
  <p><strong>Step 3:</strong> <a class="button" href="{{ url_for('status') }}">Status (auto-polls)</a> → fetch → screensaver</p>
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
     <span class="muted"> (allow pop‑ups if blocked)</span></p>
  <p><a class="button" href="{{ picker_uri }}">Open Picker in this tab</a></p>
{% else %}
  <p><em>No picker URI found; please <a href="{{ url_for('create_session') }}">create a new session</a>.</em></p>
{% endif %}
{% if session_id %}<p>Session ID: <code>{{ session_id }}</code></p>{% endif %}
<p class="muted">In Google Photos: search your album → open it → <strong>select photos/videos</strong> → press <strong>Done</strong>.</p>
{% with messages = get_flashed_messages() %}{% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}{% endwith %}
<div id="debug"></div>
<script>
async function poll(){
  try{
    const r = await fetch('/api/poll');
    document.getElementById('debug').textContent='Polling… '+new Date().toLocaleTimeString();
    if(!r.ok) throw new Error('poll failed '+r.status);
    const j = await r.json();
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

/* FILL SCREEN: photos/videos occupy the entire viewport */
img,video{
  width:100vw; height:100vh;
  object-fit:scale-down;       /* crop to fill; no letterboxing */
  background:#000;
}

.fade{animation:fade .6s ease}@keyframes fade{from{opacity:0}to{opacity:1}}
.empty{color:#ccc;font-family:system-ui,sans-serif}
#log{position:fixed;left:8px;bottom:8px;color:#888;font:12px ui-monospace,monospace;max-width:95vw;white-space:pre-wrap}

/* Hidden YouTube music player (>=200x200 per IFrame API guidance) */
#yt-sound {
  position:fixed; left:-10000px; top:-10000px;
  width:300px; height:300px; opacity:0; pointer-events:none;
}

/* Controls overlay */
#yt-controls {
  position:fixed; left:12px; bottom:12px;
  display:flex; align-items:center; gap:8px;
  background:rgba(0,0,0,0.55);
  border:1px solid rgba(255,255,255,0.15);
  border-radius:10px;
  padding:8px 10px;
  color:#eee; font:13px system-ui, sans-serif;
  z-index: 10000;
  transition: opacity .25s ease;
  cursor: default; /* show cursor over controls */
}
#yt-controls button {
  border:none; outline:none;
  padding:6px 8px; border-radius:6px;
  background:#1a1a1a; color:#fff;
  cursor:pointer; display:flex; align-items:center; gap:6px;
}
#yt-controls button:hover { background:#333; }
#yt-controls .spacer { width:1px; height:22px; background:#555; opacity:.4; }
#yt-controls input[type="range"] {
  width:120px;
  accent-color:#08c;
}
#yt-controls .label { max-width:220px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hide-cursor { cursor:none; }

/* Auto-hide controls after inactivity */
body.idle #yt-controls { opacity:0; pointer-events:none; }
</style>

<div class="stage"></div><div id="log"></div>
<div id="yt-sound"></div>

<div id="yt-controls">
  <button id="yt-prev"     title="Previous (Shift+Left)">⏮</button>
  <button id="yt-play"     title="Play/Pause (Space)">⏯</button>
  <button id="yt-next"     title="Next (Shift+Right)">⏭</button>
  <span class="spacer"></span>
  <button id="yt-mute"     title="Mute/Unmute (M)">🔈</button>
  <input  id="yt-volume"   type="range" min="0" max="100" step="1" title="Volume">
  <span class="spacer"></span>
  <span  id="yt-label"     class="label" title="Now playing">Now playing…</span>
  <!-- Fullscreen toggle with SVG icons -->
  <span class="spacer"></span>
  <button id="fs-toggle"   title="Fullscreen / Minimize" aria-label="Toggle fullscreen">
    <!-- ENTER FULLSCREEN ICON -->
    <svg id="fs-icon-enter" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path stroke="currentColor" stroke-width="2" stroke-linecap="round"
        d="M3 10V3h7M21 14v7h-7M10 21H3v-7M21 3v7"/>
    </svg>
    <!-- EXIT FULLSCREEN ICON -->
    <svg id="fs-icon-exit" width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true" style="display:none">
      <path stroke="currentColor" stroke-width="2" stroke-linecap="round"
        d="M7 7h4V3M17 17h-4v4M7 17h4v4M17 7h-4V3"/>
    </svg>
  </button>
</div>

<!-- YouTube IFrame API -->
<script src="https://www.youtube.com/iframe_api"></script>

<script>
document.addEventListener('DOMContentLoaded', async () => {
  // Best-effort: enter FS on load (Chromium --kiosk may already force fullscreen)
  try {
    const el=document.documentElement;
    if(!document.fullscreenElement && el.requestFullscreen){
      await el.requestFullscreen({navigationUI:'hide'});
    }
  } catch(e) {}

  const ITEMS = {{ items|tojson }};
  const INTERVAL = {{ interval_seconds }} * 10000;
  const REFRESH_MS = {{ refresh_minutes }} * 60 * 1000;
  const stage = document.querySelector('.stage');
  const log = document.getElementById('log');
  function logMsg(m){ log.textContent = m; }

  if(!ITEMS || !ITEMS.length){
    stage.innerHTML='<div class="empty">No items selected yet.<br/>Go back and pick photos.</div>';
    logMsg('No ITEMS'); return;
  }

  // Use real viewport size (no hard clamp)
  function getWH(){
    const w = Math.max(1, Math.round(window.innerWidth || 800));
    const h = Math.max(1, Math.round(window.innerHeight || 480));
    return {w,h};
  }

  function isVideo(it){ const mt=(it.mimeType||'').toLowerCase(); return mt.startsWith('video/') || mt.includes('motion'); }
  function urlFor(it, kind){
    const {w,h} = getWH();
    const idx = ITEMS.indexOf(it);
    const q = new URLSearchParams({kind, w:String(w), h:String(h)});
    return '/content/'+idx+'?'+q.toString();
  }

  let idx = 0;
  async function show(i){
    const it = ITEMS[i]; if(!it) return;
    stage.innerHTML = '';
    let el, kind = isVideo(it) ? 'video' : 'image';
    const url = urlFor(it, kind);

    const onError = async () => {
      const fallback = urlFor(it, 'image');
      const img = document.createElement('img');
      img.src = fallback; img.alt = it.filename||'';
      img.addEventListener('error', ()=> logMsg('Fallback image error: '+fallback));
      img.addEventListener('load',  ()=> logMsg('Fallback image loaded: '+(it.filename||'')));
      stage.innerHTML=''; stage.appendChild(img);
    };

    if(kind === 'video'){
      el = document.createElement('video');
      el.src = url; el.autoplay = true; el.loop = true; el.muted = true; el.playsInline = true;
      el.addEventListener('error', async ()=>{ logMsg('Video error: '+url); await onError(); });
      el.addEventListener('loadeddata', ()=> logMsg('Playing video '+(it.filename||'')));
    }else{
      el = document.createElement('img');
      el.src = url; el.alt = it.filename||'';
      el.addEventListener('error', async ()=>{ logMsg('Image error: '+url); await onError(); });
      el.addEventListener('load', ()=> logMsg('Showing image '+(it.filename||'')));
    }

    el.className='fade'; stage.appendChild(el);
  }

  await show(idx);
  setInterval(async () => { idx=(idx+1)%ITEMS.length; await show(idx); }, INTERVAL);
  if(REFRESH_MS>0) setInterval(()=>{ logMsg('Refreshing to renew baseUrl…'); location.reload(); }, REFRESH_MS);
});
</script>

<!-- YouTube music player with controls and auto-skip watchdog -->
<script>
  const YT_VIDEO_ID    = {{ yt_video_id|tojson }};
  const YT_PLAYLIST_ID = {{ yt_playlist_id|tojson }};
  const YT_VOLUME      = {{ yt_volume|tojson }};
  const YT_HIDE_VIDEO  = {{ yt_hide_video|tojson }};

  const $play   = document.getElementById('yt-play');
  const $prev   = document.getElementById('yt-prev');
  const $next   = document.getElementById('yt-next');
  const $mute   = document.getElementById('yt-mute');
  const $vol    = document.getElementById('yt-volume');
  const $label  = document.getElementById('yt-label');

  let ytPlayer = null;
  let labelPollT = null;
  let labelPollTries = 0;

  // --- Auto-skip watchdog: if not PLAYING within timeout, skip to next ---
  let playWatchdogT = null;
  const WATCHDOG_MS = 10000;  // wait up to 10s for PLAYING

  function armPlayWatchdog() {
    clearTimeout(playWatchdogT);
    playWatchdogT = setTimeout(() => {
      try {
        const st = ytPlayer.getPlayerState();
        if (st !== YT.PlayerState.PLAYING) {
          console.warn('Watchdog: not playing after', WATCHDOG_MS, 'ms -> nextVideo()');
          ytPlayer.nextVideo();
        }
      } catch(e) {
        console.warn('Watchdog check failed:', e);
      }
    }, WATCHDOG_MS);
  }
  function disarmPlayWatchdog() {
    clearTimeout(playWatchdogT);
    playWatchdogT = null;
  }

  function setPlayingUI(playing) { $play.textContent = playing ? '⏸' : '⏯'; }
  function setMuteUI(muted)      { $mute.textContent = muted ? '🔇' : '🔈'; }

  function fallbackLabel() {
    try {
      const idx = ytPlayer.getPlaylistIndex();
      const list = ytPlayer.getPlaylist() || [];
      if (idx != null && idx >= 0 && idx < list.length) {
        return `Track ${idx+1}`;
      }
      const url = ytPlayer.getVideoUrl();
      const m = /[?&]v=([^&]+)/.exec(url);
      return m ? `Video ${m[1]}` : 'Now playing…';
    } catch { return 'Now playing…'; }
  }

  function updateLabel(forceFallback=false) {
    let title = '';
    try { const d = ytPlayer.getVideoData(); title = (d && d.title) ? d.title.trim() : ''; } catch {}
    $label.textContent = (title && !forceFallback) ? title : fallbackLabel();
  }

  function startLabelPoll() {
    clearInterval(labelPollT);
    labelPollTries = 0;
    labelPollT = setInterval(() => {
      labelPollTries++;
      updateLabel();
      if (labelPollTries >= 10) clearInterval(labelPollT);
    }, 2000);
  }

  function onYouTubeIframeAPIReady() {
    const baseVars = { autoplay:1, controls:0, disablekb:1, modestbranding:1, rel:0, fs:0, playsinline:1, loop:1, origin:location.origin };
    let playerVars = { ...baseVars };

    if (YT_VIDEO_ID && !YT_PLAYLIST_ID) {
      playerVars.playlist = YT_VIDEO_ID; // loop single video
      ytPlayer = new YT.Player('yt-sound', {
        width:200, height:200, videoId:YT_VIDEO_ID, playerVars,
        events:{ onReady:onYtReady, onStateChange:onYtState, onError:onYtError }
      });
    } else if (YT_PLAYLIST_ID) {
      ytPlayer = new YT.Player('yt-sound', {
        width:200, height:200, playerVars:{ ...playerVars, listType:'playlist', list:YT_PLAYLIST_ID },
        events:{ onReady:onYtReady, onStateChange:onYtState, onError:onYtError }
      });
    }
  }
  window.onYouTubeIframeAPIReady = onYouTubeIframeAPIReady;

  function onYtReady() {
    try {
      ytPlayer.setVolume(Math.max(0, Math.min(100, Number(YT_VOLUME) || 60)));
      $vol.value = ytPlayer.getVolume();
      setMuteUI(ytPlayer.isMuted());
      updateLabel(true);
      startLabelPoll();

      ytPlayer.unMute();
      ytPlayer.playVideo();

      // Arm watchdog to ensure we reach PLAYING
      armPlayWatchdog();

      // Fallback: try muted then unmute (handles autoplay policies)
      let tries = 0;
      const attemptUnmute = () => { try { ytPlayer.unMute(); setMuteUI(false); } catch {} if (++tries < 5) setTimeout(attemptUnmute, 3000); };
      setTimeout(() => {
        const state = ytPlayer.getPlayerState();
        if (state !== YT.PlayerState.PLAYING) {
          ytPlayer.mute(); setMuteUI(true);
          ytPlayer.playVideo();
          setTimeout(attemptUnmute, 2000);
        }
      }, 800);
    } catch(e) { console.warn('YouTube init error:', e); }
  }

  function onYtState(e) {
    const st = e.data;
    const playing = st === YT.PlayerState.PLAYING;
    setPlayingUI(playing);

    if (st === YT.PlayerState.PLAYING) {
      // Playing: cancel watchdog
      disarmPlayWatchdog();
      updateLabel(); startLabelPoll();
    } else if (st === YT.PlayerState.BUFFERING || st === YT.PlayerState.UNSTARTED) {
      // Buffering/unstarted: (re)arm watchdog
      armPlayWatchdog();
      updateLabel(); startLabelPoll();
    } else if (st === YT.PlayerState.ENDED) {
      // Let YT loop/advance; re-arm watchdog just in case
      armPlayWatchdog();
    } else if (st === YT.PlayerState.PAUSED || st === YT.PlayerState.CUED) {
      if (st === YT.PlayerState.PAUSED) {
        disarmPlayWatchdog();
      } else {
        armPlayWatchdog();
      }
    }
  }

  function onYtError(e) {
    console.error('YouTube player error', e);
    try {
      ytPlayer.nextVideo();      // immediately skip problematic track
      armPlayWatchdog();         // ensure the next one reaches PLAYING
    } catch(err) {
      console.warn('Failed to advance on error:', err);
    }
  }

  $play.addEventListener('click', () => {
    try { const st = ytPlayer.getPlayerState(); if (st === YT.PlayerState.PLAYING) ytPlayer.pauseVideo(); else ytPlayer.playVideo(); } catch {}
  });
  $prev.addEventListener('click', () => { try { ytPlayer.previousVideo(); armPlayWatchdog(); } catch {} });
  $next.addEventListener('click', () => { try { ytPlayer.nextVideo(); armPlayWatchdog(); } catch {} });
  $mute.addEventListener('click', () => { try { if (ytPlayer.isMuted()) { ytPlayer.unMute(); setMuteUI(false); } else { ytPlayer.mute(); setMuteUI(true); } } catch {} });
  $vol.addEventListener('input', () => { try { ytPlayer.setVolume(Number($vol.value)); } catch {} });

  document.addEventListener('keydown', (ev) => {
    if (ev.code === 'Space') { ev.preventDefault(); $play.click(); }
    else if (ev.key.toLowerCase() === 'm') { $mute.click(); }
    else if (ev.shiftKey && ev.code === 'ArrowLeft') { $prev.click(); }
    else if (ev.shiftKey && ev.code === 'ArrowRight') { $next.click(); }
  });

  // Auto-hide controls after inactivity
  let idleT = null;
  function bumpActivity() {
    document.body.classList.remove('idle');
    if (idleT) clearTimeout(idleT);
    idleT = setTimeout(() => document.body.classList.add('idle'), 3000);
  }
  ['mousemove','mousedown','keydown','touchstart'].forEach(ev =>
    document.addEventListener(ev, bumpActivity, {passive:true})
  );
  bumpActivity();
</script>

<!-- Fullscreen toggle logic with SVG icon swap -->
<script>
  function isFullscreen(){ return !!document.fullscreenElement; }
  async function enterFullscreen(){ try{ await document.documentElement.requestFullscreen({navigationUI:'hide'}); }catch(e){ console.warn('requestFullscreen failed:', e); } }
  async function exitFullscreen(){ try{ await document.exitFullscreen(); }catch(e){ console.warn('exitFullscreen failed:', e); } }

  const fsBtn     = document.getElementById('fs-toggle');
  const enterIcon = document.getElementById('fs-icon-enter');
  const exitIcon  = document.getElementById('fs-icon-exit');

  function updateFsUI(){
    const on = isFullscreen();
    if (fsBtn){
      fsBtn.title = on ? 'Minimize (exit fullscreen)' : 'Fullscreen';
    }
    if (enterIcon && exitIcon){
      enterIcon.style.display = on ? 'none' : '';
      exitIcon.style.display  = on ? '' : 'none';
    }
  }

  if (!fsBtn) {
    console.warn('fs-toggle button not found in DOM');
  } else {
    fsBtn.addEventListener('click', async () => {
      try {
        if (isFullscreen()) await exitFullscreen();
        else await enterFullscreen();
      } catch (e) {
        console.warn('Fullscreen toggle failed:', e);
      } finally {
        updateFsUI();
      }
    });
    document.addEventListener('fullscreenchange', updateFsUI);
    updateFsUI();
  }

  // Keyboard shortcut: F toggles fullscreen
  document.addEventListener('keydown', (e) => {
    if (e.key.toLowerCase() === 'f' && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      fsBtn?.click();
    }
  });
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
<h1>Diagnostics: first {{ count }} items (proxied)</h1>
{% for it in items %}
  <div class="item">
    <div><strong>{{ loop.index }}.</strong> {{ it.filename or '(no filename)' }} — {{ it.mimeType }}</div>
    <div>Proxied image URL: <a class="link" href="{{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480" target="_blank" rel="noopener">{{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480</a></div>
    <div>Proxied video URL: <a class="link" href="{{ url_for('content', index=loop.index0) }}?kind=video" target="_blank" rel="noopener">{{ url_for('content', index=loop.index0) }}?kind=video</a></div>
    <div style="margin-top:.5rem">
      {% if it.mimeType and it.mimeType.lower().startswith('video') %}
        <video src="{{ url_for('content', index=loop.index0) }}?kind=video" controls muted playsinline></video>
      {% else %}
        <img src="{{ url_for('content', index=loop.index0) }}?kind=image&w=800&h=480" alt="{{ it.filename or '' }}" />
      {% endif %}
    </div>
  </div>
{% endfor %}
<pre>{{ items|tojson(indent=2) }}</pre>
"""


# -------------------------- routes --------------------------
@app.route("/")
def home():
    return redirect(url_for("pick"))

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

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
        app.logger.error("OAuth callback: missing 'code'")
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

    if not session["access_token"]:
        app.logger.error("No access_token in token response: %s", tok)
        flash("Authorization failed: no access token returned.")
        return redirect(url_for("pick"))

    # Persist tokens for server-side kiosk use
    save_tokens(tok)

    return redirect(url_for("create_session"))

# ---- Picker session lifecycle ----
@app.route("/create-session", methods=["GET", "POST"])
def create_session():
    access_token = session.get("access_token")
    if not access_token:
        flash("Not authorized. Please start authorization.")
        return redirect(url_for("auth_start"))

    url = f"{PICKER_BASE}/sessions"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.post(url, headers=headers, json={}, timeout=20)
        if resp.status_code != 200:
            app.logger.error("Create session failed: %s %s", resp.status_code, resp.text)
            flash("Failed to create Picker session. Is the Photos Picker API enabled & scope correct?")
            return redirect(url_for("pick"))
        data = resp.json()
        app.logger.info("sessions.create response: %s", data)
    except Exception:
        app.logger.exception("Create session exception")
        flash("Network error creating Picker session.")
        return redirect(url_for("pick"))

    session_id = data.get("id")
    picker_uri = data.get("pickerUri")
    if not (session_id and picker_uri):
        app.logger.error("Create session missing fields: %s", data)
        flash("Picker session created but missing data. Try again.")
        return redirect(url_for("pick"))

    session["picker_session_id"] = session_id
    session["picker_uri"] = picker_uri
    return redirect(url_for("status"))

@app.route("/api/picker/session", methods=["POST"])
def api_picker_session():
    return create_session()

@app.route("/status")
def status():
    picker_uri = session.get("picker_uri")
    if picker_uri:
        picker_uri = picker_uri.rstrip("/") + "/autoclose"
    return render_template_string(STATUS_TEMPLATE, picker_uri=picker_uri, session_id=session.get("picker_session_id"))

@app.route("/api/poll")
def api_poll():
    access_token = session.get("access_token"); session_id = session.get("picker_session_id")
    if not (access_token and session_id):
        return jsonify({"ready": False, "interval": 5.0, "error": "no_session"}), 200
    headers = {"Authorization": f"Bearer {access_token}"}
    status_url = f"{PICKER_BASE}/sessions/{session_id}"
    try:
        r = requests.get(status_url, headers=headers, timeout=20)
        if r.status_code != 200:
            app.logger.error("Poll status failed: %s %s", r.status_code, r.text)
            return jsonify({"ready": False, "interval": 5.0, "error": f"http_{r.status_code}"}), 200
        info = r.json()
        if info.get("mediaItemsSet"):
            return jsonify({"ready": True, "interval": 0}), 200
        poll_cfg = info.get("pollingConfig", {})
        interval = parse_seconds(poll_cfg.get("pollInterval"), default=5.0)
        return jsonify({"ready": False, "interval": interval}), 200
    except Exception:
        app.logger.exception("Poll exception")
        return jsonify({"ready": False, "interval": 5.0, "error": "exception"}), 200

@app.route("/poll-till-ready")
def poll_till_ready_compat():
    access_token = session.get("access_token"); session_id = session.get("picker_session_id")
    if not (access_token and session_id): return redirect(url_for("create_session"))
    headers = {"Authorization": f"Bearer {access_token}"}
    status_url = f"{PICKER_BASE}/sessions/{session_id}"
    try:
        r = requests.get(status_url, headers=headers, timeout=20)
        if r.status_code != 200: app.logger.error("Compat poll failed: %s %s", r.status_code, r.text); return ("", 204)
        info = r.json()
        if info.get("mediaItemsSet"): return redirect(url_for("fetch_selected"))
        return ("", 204)
    except Exception:
        app.logger.exception("Compat poll exception"); return ("", 204)

@app.route("/fetch-selected")
def fetch_selected():
    access_token = session.get("access_token"); session_id = session.get("picker_session_id")
    if not (access_token and session_id):
        flash("No active Picker session. Create session first."); return redirect(url_for("create_session"))

    headers = {"Authorization": f"Bearer {access_token}"}
    items_url = f"{PICKER_BASE}/mediaItems"
    all_items, page_token = [], None
    try:
        while True:
            params = {"sessionId": session_id, "pageSize": 100}
            if page_token: params["pageToken"] = page_token
            r = requests.get(items_url, headers=headers, params=params, timeout=20)
            if r.status_code != 200:
                app.logger.error("List items failed: %s %s", r.status_code, r.text)
                flash("Failed to list picked items. Try polling again."); return redirect(url_for("status"))
            data = r.json()
            all_items.extend(data.get("mediaItems", []) or [])
            page_token = data.get("nextPageToken")
            if not page_token: break
    except Exception:
        app.logger.exception("Fetch selected exception")
        flash("Network error fetching selected items."); return redirect(url_for("status"))

    app.logger.info("Picked items count: %d", len(all_items))

    simplified = []
    for m in all_items:
        mf = m.get("mediaFile") or {}
        base = mf.get("baseUrl") or m.get("baseUrl")
        mime = mf.get("mimeType", m.get("mimeType", ""))
        filename = mf.get("filename", m.get("filename", ""))
        if not base: continue
        simplified.append({"baseUrl": base, "mimeType": mime, "filename": filename})

    if len(simplified) == 0:
        flash("No items selected. In Google Photos: search the album, open it, select photos/videos, then press Done.")
        return redirect(url_for("status"))

    try: save_media_items(simplified)
    except Exception:
        app.logger.exception("Failed to save selected_media.json")
        flash("Failed to save selection locally (permission?)."); return redirect(url_for("screensaver"))

    flash(f"Fetched {len(simplified)} selected items.")
    return redirect(url_for("screensaver"))

# ---- Media proxy (fixes 403 in kiosk browser) ----
@app.route("/content/<int:index>")
def content(index: int):
    items = load_media_items()
    if index < 0 or index >= len(items):
        abort(404)
    item = items[index]
    kind = request.args.get("kind", "image")  # 'image' or 'video'
    try:
        w = int(request.args.get("w", "800"))
        h = int(request.args.get("h", "480"))
    except ValueError:
        w, h = 800, 480

    url = build_media_url(item, kind, w=w, h=h)
    if not url:
        abort(404)

    # Prefer session token (remote browsers that did OAuth)
    access_token = session.get("access_token")

    # If no session token AND the request is local kiosk, use server-side token
    is_local = request.remote_addr in ("127.0.0.1", "::1")
    if (not access_token) and is_local and DISABLE_SESSION_AUTH_FOR_LOCAL:
        access_token = get_server_access_token()

    if not access_token:
        abort(401)

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            app.logger.error("Proxy fetch failed (%s): %s", r.status_code, url)
            abort(r.status_code)
        ctype = r.headers.get("Content-Type", "application/octet-stream")
        resp = Response(r.content, status=200, mimetype=ctype)
        resp.headers["Cache-Control"] = "private, max-age=1800"
        return resp
    except Exception:
        app.logger.exception("Proxy fetch exception for %s", url)
        abort(502)

@app.route("/screensaver")
def screensaver():
    items = load_media_items()
    try: interval_seconds = int(request.args.get("interval", ADVANCE_SECONDS_DEFAULT))
    except ValueError: interval_seconds = ADVANCE_SECONDS_DEFAULT
    try: refresh_minutes = int(request.args.get("refresh", REFRESH_MINUTES_DEFAULT))
    except ValueError: refresh_minutes = REFRESH_MINUTES_DEFAULT
    return render_template_string(
        SCREENSAVER_TEMPLATE,
        items=items,
        interval_seconds=interval_seconds,
        refresh_minutes=refresh_minutes,
        yt_video_id=YT_VIDEO_ID,
        yt_playlist_id=YT_PLAYLIST_ID,
        yt_volume=YT_VOLUME,
        yt_hide_video=YT_HIDE_VIDEO,
    )

@app.route("/diag")
def diag():
    items = load_media_items()
    count = min(5, len(items))
    return render_template_string(DIAG_TEMPLATE, items=items[:count], count=count)

@app.route("/auth/signout")
def auth_signout():
    session.clear(); flash("Signed out."); return redirect(url_for("pick"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

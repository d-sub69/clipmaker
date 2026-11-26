"""
tiktok_upload.py - send finished parts to your TikTok *drafts* (inbox) via the free
Content Posting API ("Upload" mode, scope video.upload). stdlib only.

TikTok does not accept a caption in this mode: the video lands in your TikTok app
notifications/inbox as a draft, and you paste partNN.txt there yourself.

One-time setup (see README): create an app at developers.tiktok.com, add the products
"Login Kit" + "Content Posting API", scope video.upload, redirect URI
    http://127.0.0.1:8765/tiktok/callback
then paste Client key / Client secret into the UI and press "Connect TikTok".
"""
import hashlib
import json
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://open.tiktokapis.com"
REDIRECT = "http://127.0.0.1:8765/tiktok/callback"
MIN_CHUNK, MAX_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024

_pending: dict = {}           # state -> code_verifier (in memory, one login at a time)


class TikTokError(Exception):
    pass


class Store:
    """Keys + tokens in <root>\\tiktok_auth.json (outside the code folder)."""
    def __init__(self, root: Path):
        self.path = Path(root) / "tiktok_auth.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self, **kw):
        d = self.load()
        d.update(kw)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _http(method, url, data=None, headers=None, raw=False, timeout=300):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            msg = json.loads(body)
            e = msg.get("error", {}) if isinstance(msg.get("error"), dict) else {}
            msg = f"{e.get('code')}: {e.get('message')}" if e.get("code") else (msg.get("error_description") or msg)
            if "unaudited_client_can_only_post_to_private_accounts" in str(msg):
                msg = ("TikTok only lets an unaudited app post from a PRIVATE account. "
                       "Set your TikTok account to private (Settings and privacy > Privacy > Private account), "
                       "post, then switch it back. [" + str(msg) + "]")
        except Exception:
            msg = body[:300]
        raise TikTokError(f"HTTP {e.code}: {msg}")
    return body if raw else json.loads(body)


def _form(url, fields):
    return _http("POST", url, urllib.parse.urlencode(fields).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})


# ------------------------------------------------------------------ login
def auth_url(store: Store) -> str:
    c = store.load()
    if not c.get("client_key") or not c.get("client_secret"):
        raise TikTokError("Enter Client key and Client secret first")
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    _pending[state] = verifier
    q = {"client_key": c["client_key"], "scope": "user.info.basic,video.upload,video.publish", "response_type": "code",
         "redirect_uri": REDIRECT, "state": state,
         # TikTok wants the HEX sha256 of the verifier (not base64url like standard PKCE)
         "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
         "code_challenge_method": "S256"}
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(q)


def finish_login(store: Store, code: str, state: str):
    verifier = _pending.pop(state, None)
    if not verifier:
        raise TikTokError("Unknown or expired login attempt - press Connect again")
    c = store.load()
    t = _form(f"{API}/v2/oauth/token/", {
        "client_key": c["client_key"], "client_secret": c["client_secret"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": REDIRECT, "code_verifier": verifier})
    _save_tokens(store, t)


def _save_tokens(store, t):
    if "access_token" not in t:
        raise TikTokError(t.get("error_description") or str(t))
    store.save(access_token=t["access_token"], refresh_token=t.get("refresh_token"),
               expires_at=time.time() + int(t.get("expires_in", 86400)) - 120, open_id=t.get("open_id"))


def access_token(store: Store) -> str:
    c = store.load()
    if not c.get("access_token"):
        raise TikTokError("Not connected - press Connect TikTok")
    if time.time() >= c.get("expires_at", 0):
        t = _form(f"{API}/v2/oauth/token/", {
            "client_key": c["client_key"], "client_secret": c["client_secret"],
            "grant_type": "refresh_token", "refresh_token": c.get("refresh_token", "")})
        _save_tokens(store, t)
        c = store.load()
    return c["access_token"]


def connected(store: Store) -> bool:
    return bool(store.load().get("access_token"))


# ------------------------------------------------------------------ upload
def _send(store: Store, video: Path, endpoint: str, post_info=None, progress=lambda pct: None) -> str:
    tok = access_token(store)
    size = video.stat().st_size
    if size <= MAX_CHUNK:
        chunk, count = size, 1                      # whole file in one request
    else:
        chunk = 32 * 1024 * 1024                    # 5..64 MB each
        count = math.floor(size / chunk)            # last chunk absorbs the remainder (< 2 x chunk)
    body = {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk, "total_chunk_count": count}}
    if post_info:
        body["post_info"] = post_info
    r = _http("POST", f"{API}{endpoint}", json.dumps(body).encode(),
              {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"})
    err = r.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message')}")
    pid, url = r["data"]["publish_id"], r["data"]["upload_url"]

    with open(video, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            _http("PUT", url, data, {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                                     "Content-Range": f"bytes {start}-{end}/{size}"}, raw=True, timeout=1800)
            progress(int(100 * (end + 1) / size))
    return pid


def upload_draft(store: Store, video: Path, progress=lambda pct: None) -> str:
    """Send one mp4 to the user's TikTok inbox (they finish it in the app)."""
    return _send(store, video, "/v2/post/publish/inbox/video/init/", None, progress)


def creator_info(store: Store) -> dict:
    r = _http("POST", f"{API}/v2/post/publish/creator_info/query/", b"{}",
              {"Authorization": f"Bearer {access_token(store)}", "Content-Type": "application/json; charset=UTF-8"})
    err = r.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise TikTokError(f"{err.get('code')}: {err.get('message')}")
    return r["data"]


def direct_post(store: Store, video: Path, post_info: dict, progress=lambda pct: None) -> str:
    """Post one mp4 directly to the user's profile (Direct Post, scope video.publish)."""
    return _send(store, video, "/v2/post/publish/video/init/", post_info, progress)


def publish_status(store: Store, publish_id: str) -> str:
    r = _http("POST", f"{API}/v2/post/publish/status/fetch/", json.dumps({"publish_id": publish_id}).encode(),
              {"Authorization": f"Bearer {access_token(store)}", "Content-Type": "application/json; charset=UTF-8"})
    return r.get("data", {}).get("status", "UNKNOWN")

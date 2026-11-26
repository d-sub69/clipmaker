#!/usr/bin/env python3
"""
app.py - local desktop UI for the TikTok clip maker (stdlib only, no extra installs).

    python app.py                 # open the UI
    python app.py "D:\\video.mp4"  # queue a video, then open the UI
    python app.py "D:\\video.mp4" "@creator"

The UI only drops files into 01_inbox and reads pipeline.db / the log; all the work is
done by pipeline.py (started in the background if it isn't already running).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline as pl
import tiktok_upload as tt

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("TIKTOK_CONFIG", HERE / "config.toml"))
PORT = 8765
cfg = pl.load_config(CONFIG)
con_lock = threading.Lock()

SETTINGS = {  # ui key -> (section, key, type)
    "interval": ("split", "interval_seconds", int),
    "model": ("whisper", "model", str),
    "language": ("whisper", "language", str),
    "device": ("whisper", "device", str),
    "fit": ("video", "fit", str),
    "credit": ("text", "default_credit", str),
}


def db():
    return pl.db_connect(cfg)


# ------------------------------------------------------------------ TikTok drafts
store = tt.Store(cfg["_dirs"]["inbox"].parent)
uploads: dict[str, dict] = {}       # output folder -> {"state": "uploading"|"done"|"error", "label": str}


def _sent_file(out: Path) -> Path:
    return out / "_tiktok.json"


def _send_worker(out: Path):
    key = str(out)
    sent = {}
    if _sent_file(out).exists():
        sent = json.loads(_sent_file(out).read_text(encoding="utf-8"))
    parts = sorted(out.glob("part*.mp4"))
    try:
        for n, mp4 in enumerate(parts, 1):
            if mp4.name in sent:
                continue
            uploads[key] = {"state": "uploading", "label": f"sending {mp4.name} ({n}/{len(parts)})..."}
            sent[mp4.name] = tt.upload_draft(store, mp4, lambda p, n=n, m=mp4: uploads[key].update(
                label=f"sending {m.name} ({n}/{len(parts)}) {p}%"))
            _sent_file(out).write_text(json.dumps(sent), encoding="utf-8")
            time.sleep(11)                      # API limit: 6 init calls / minute
        uploads[key] = {"state": "done", "label": f"{len(parts)} part(s) sent - "
                        "open the TikTok app, tap each notification, add the caption, post or save to drafts"}
    except Exception as e:
        uploads[key] = {"state": "error", "label": str(e)}


def _post_worker(out: Path, mp4: Path, post_info: dict):
    key = str(out)
    try:
        uploads[key] = {"state": "uploading", "label": f"posting {mp4.name}..."}
        pid = tt.direct_post(store, mp4, post_info, lambda p: uploads[key].update(
            label=f"posting {mp4.name} {p}%"))
        for _ in range(40):                         # TikTok processes it for a minute or two
            st = tt.publish_status(store, pid)
            uploads[key] = {"state": "uploading", "label": f"{mp4.name}: TikTok status {st}"}
            if st == "PUBLISH_COMPLETE":
                uploads[key] = {"state": "done", "label": f"{mp4.name} posted ({post_info['privacy_level']})"}
                return
            if st == "FAILED":
                raise tt.TikTokError("TikTok reported FAILED")
            time.sleep(5)
        uploads[key] = {"state": "done", "label": f"{mp4.name} sent, TikTok is still processing it"}
    except Exception as e:
        uploads[key] = {"state": "error", "label": str(e)}


def direct_post_part(d: dict):
    out = Path(d["path"]).resolve()
    if out.parent != cfg["_dirs"]["output"].resolve() or not out.is_dir():
        raise ValueError("Not a finished output folder")
    mp4 = out / Path(d["part"]).name
    if mp4.suffix != ".mp4" or not mp4.is_file():
        raise ValueError("Part not found")
    if not d.get("privacy"):
        raise ValueError("Choose who can view the video")
    if uploads.get(str(out), {}).get("state") == "uploading":
        raise ValueError("An upload is already running for this job")
    branded = d.get("commercial") and d.get("branded")
    if d.get("commercial") and not (d.get("your_brand") or d.get("branded")):
        raise ValueError("Pick at least one commercial-content option")
    if branded and d["privacy"] == "SELF_ONLY":
        raise ValueError("Branded content can't be private")
    info = {"title": d.get("title", "")[:2200], "privacy_level": d["privacy"],
            "disable_comment": not d.get("comment"), "disable_duet": not d.get("duet"),
            "disable_stitch": not d.get("stitch"),
            "brand_content_toggle": bool(branded),
            "brand_organic_toggle": bool(d.get("commercial") and d.get("your_brand"))}
    tt.access_token(store)
    uploads[str(out)] = {"state": "uploading", "label": "starting..."}
    threading.Thread(target=_post_worker, args=(out, mp4, info), daemon=True).start()


def parts_json(path: str) -> dict:
    out = Path(path).resolve()
    if out.parent != cfg["_dirs"]["output"].resolve() or not out.is_dir():
        raise ValueError("Not a finished output folder")
    res = []
    for mp4 in sorted(out.glob("part*.mp4")):
        t = mp4.with_suffix(".txt")
        res.append({"name": mp4.name, "text": t.read_text(encoding="utf-8") if t.exists() else ""})
    return {"parts": res}


def send_to_tiktok(path: str):
    out = Path(path).resolve()
    if out.parent != cfg["_dirs"]["output"].resolve() or not out.is_dir():
        raise ValueError("Not a finished output folder")
    if uploads.get(str(out), {}).get("state") == "uploading":
        return
    tt.access_token(store)                       # fail fast if not connected
    uploads[str(out)] = {"state": "uploading", "label": "starting..."}
    threading.Thread(target=_send_worker, args=(out,), daemon=True).start()


# ------------------------------------------------------------------ watcher
NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW: never flash a console window
PID_FILE = cfg["_dirs"]["inbox"].parent / "watcher.pid"


def _pid_alive(pid: int) -> bool:
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259                       # STILL_ACTIVE


def _watcher_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if _pid_alive(pid) else None
    except Exception:
        return None


def watcher_running() -> bool:
    return _watcher_pid() is not None


def stop_watcher():
    pid = _watcher_pid()
    if pid:
        os.kill(pid, 9)
        time.sleep(1)


def start_watcher():
    if watcher_running():
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(HERE / "pipeline.py"), "run"],
                     cwd=HERE, creationflags=NO_WINDOW)


# ------------------------------------------------------------------ queueing
def queue_video(path: str, credit: str = "") -> str:
    src = Path(path.strip().strip('"'))
    if not src.is_file():
        raise ValueError(f"File not found: {src}")
    if src.suffix.lower() not in pl.VIDEO_EXT:
        raise ValueError(f"Not a supported video type: {src.suffix}")
    credit = credit.strip()
    dest_dir = cfg["_dirs"]["inbox"] / (credit if credit else "")
    if credit and not credit.startswith("@"):
        dest_dir = cfg["_dirs"]["inbox"] / ("@" + credit)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    n = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    tmp = dest.with_suffix(".part")          # watcher ignores .part files until the copy is done
    shutil.copy2(src, tmp)                   # copy2 keeps old mtime -> picked up right away
    os.replace(tmp, dest)
    return dest.name


# ------------------------------------------------------------------ status
def log_lines(n=400):
    p = cfg["_dirs"]["logs"] / "pipeline.log"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def progress_for(job, lines):
    if job["status"] == "done":
        return 100, f"{job['parts']} part(s) ready"
    if job["status"] == "failed":
        return 0, "failed"
    tag = f"[{job['id']}]"
    mine = [l for l in lines if tag in l]
    if not mine:
        return 0, "waiting"
    last = mine[-1]
    m = re.search(r"rendering part (\d+)/(\d+)", last) or re.search(r"part (\d+)/(\d+) done", last)
    if m:
        i, t = int(m[1]), int(m[2])
        done = i if "done" in last else i - 1
        return 30 + int(65 * done / t), f"rendering part {i}/{t}"
    if "-> " in last and "part(s)" in last:
        return 30, "splitting"
    if "transcribed" in last or "existing transcript" in last:
        return 28, "transcribed"
    if "Detected language" in last:
        return 15, "transcribing audio..."
    return 5, "transcribing audio (model loading on first run)..."


def jobs_json():
    lines = log_lines()
    with con_lock:
        rows = db().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    out = []
    for j in rows:
        if j["status"] == "done" and not (j["output"] and Path(j["output"]).is_dir()):
            continue                                   # output folder was deleted
        if j["status"] == "failed" and not (j["source"] and Path(j["source"]).parent.is_dir()):
            continue                                   # failed folder was deleted
        pct, label = progress_for(j, lines)
        out.append({"id": j["id"], "name": j["name"], "status": j["status"], "credit": j["credit"],
                    "pct": pct, "label": label, "output": j["output"], "parts": j["parts"],
                    "error": (j["error"] or "")[-600:],
                    "tiktok": uploads.get(str(Path(j["output"]).resolve())) if j["output"] else None,
                    "sent": j["output"] is not None and _sent_file(Path(j["output"])).exists()})
    waiting = [p.name for p in cfg["_dirs"]["inbox"].rglob("*")
               if p.is_file() and p.suffix.lower() in pl.VIDEO_EXT]
    return {"jobs": out, "waiting": waiting, "watcher": watcher_running()}


def read_settings():
    c = pl.load_config(CONFIG)
    return {k: c[s].get(key, "") for k, (s, key, _) in SETTINGS.items()}


def write_settings(vals: dict):
    text = CONFIG.read_text(encoding="utf-8")
    for k, v in vals.items():
        if k not in SETTINGS:
            continue
        sec, key, typ = SETTINGS[k]
        v = typ(v)
        rep = str(v) if typ is int else '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
        pat = re.compile(rf'(^\[{sec}\][^\[]*?^{key}\s*=\s*)(?:"[^"]*"|\S+)', re.M | re.S)
        text, n = pat.subn(lambda m: m.group(1) + rep, text, count=1)
        if not n:
            raise ValueError(f"setting {key} not found in config.toml")
    CONFIG.write_text(text, encoding="utf-8")


def retry_failed():
    with con_lock:
        c = db()
        pl.cmd_retry(cfg, c)


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html")
        if self.path == "/api/jobs":
            return self._send(200, json.dumps(jobs_json()))
        if self.path == "/api/log":
            return self._send(200, json.dumps({"log": "\n".join(log_lines(150))}))
        if self.path == "/api/settings":
            return self._send(200, json.dumps(read_settings()))
        if self.path == "/api/tiktok/creator":
            try:
                return self._send(200, json.dumps(tt.creator_info(store)))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path.startswith("/api/parts?"):
            try:
                p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["p"][0]
                return self._send(200, json.dumps(parts_json(p)))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if self.path == "/api/tiktok":
            c = store.load()
            return self._send(200, json.dumps({"has_keys": bool(c.get("client_key") and c.get("client_secret")),
                                               "connected": tt.connected(store),
                                               "client_key": c.get("client_key", "")}))
        if self.path.startswith("/tiktok/callback"):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            try:
                if "code" not in q:
                    raise tt.TikTokError(q.get("error_description") or q.get("error") or "no code returned")
                tt.finish_login(store, q["code"], q.get("state", ""))
                msg = "TikTok connected. You can close this tab."
            except Exception as e:
                msg = f"Login failed: {e}"
            return self._send(200, f"<h3 style='font-family:sans-serif'>{msg}</h3>", "text/html")
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/add":
                added = []
                for p in data.get("paths", []):
                    if p.strip():
                        added.append(queue_video(p, data.get("credit", "")))
                start_watcher()
                return self._send(200, json.dumps({"added": added}))
            if self.path == "/api/retry":
                retry_failed()
                start_watcher()
            elif self.path == "/api/tiktok/keys":
                store.save(client_key=data["client_key"].strip(), client_secret=data["client_secret"].strip(),
                           access_token=None, refresh_token=None)
            elif self.path == "/api/tiktok/connect":
                webbrowser.open(tt.auth_url(store))
            elif self.path == "/api/tiktok/post":
                direct_post_part(data)
            elif self.path == "/api/tiktok/send":
                send_to_tiktok(data["path"])
            elif self.path == "/api/open":
                p = Path(data["path"])
                if str(p).startswith(str(cfg["_dirs"]["output"])) or p.parent == cfg["_dirs"]["output"]:
                    os.startfile(p)
            elif self.path == "/api/settings":
                write_settings(data)
                stop_watcher()
                start_watcher()
            self._send(200, "{}")
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>TikTok Clip Maker</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1d23;--mut:#6b7280;--ac:#fe2c55;--bd:#e5e7eb}
@media(prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e26;--fg:#e8eaee;--mut:#8b93a1;--bd:#2c303b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px}h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:14px}
input,select,textarea{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:8px;font:inherit}
textarea{width:100%;height:64px;resize:vertical}button{background:var(--ac);color:#fff;border:0;border-radius:6px;padding:8px 14px;font:inherit;cursor:pointer}
button.s{background:var(--bd);color:var(--fg)}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
.job{padding:10px 0;border-top:1px solid var(--bd)}.job:first-child{border:0}.bar{height:6px;background:var(--bd);border-radius:3px;margin:6px 0;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ac)}.mut{color:var(--mut);font-size:12px}.err{color:#dc2626;font-size:12px;white-space:pre-wrap}
pre{max-height:240px;overflow:auto;font-size:11px;white-space:pre-wrap}label{font-size:12px;color:var(--mut)}
</style></head><body><main><h1>TikTok Clip Maker <span class="mut" id="w"></span></h1>
<div class="card"><b>Add video</b> <span class="mut">- paste the full path (one per line) or drop files here</span>
<textarea id="paths" placeholder='D:\Videos\talk.mp4'></textarea>
<div class="row"><input id="credit" placeholder="Creator credit, e.g. @name (optional)">
<button onclick="add()">Make clips</button><span id="msg" class="mut"></span></div></div>
<div class="card"><b>Jobs</b><div id="jobs"><span class="mut">loading...</span></div>
<div class="row"><button class="s" onclick="post('/api/retry')">Retry failed</button>
<button class="s" onclick="showLog()">Show log</button></div><pre id="log" hidden></pre></div>
<div class="card"><b>TikTok upload</b> <span class="mut" id="tstat"></span>
<div class="mut">Videos arrive as notifications in your TikTok app: tap one, paste the caption from partNN.txt, then post it or save it to drafts (TikTok doesn't allow a caption through the API).
Redirect URI to register in your TikTok app: <code>http://127.0.0.1:8765/tiktok/callback</code></div>
<div class="row"><input id="ck" placeholder="Client key"><input id="cs" type="password" placeholder="Client secret">
<button class="s" onclick="saveKeys()">Save keys</button><button onclick="connect()">Connect TikTok</button></div><div id="tmsg" class="err"></div></div>
<div class="card"><b>Settings</b><div class="row" id="set"></div><div class="row"><button class="s" onclick="save()">Save &amp; restart engine</button><span id="smsg" class="mut"></span></div></div>
</main><script>
const $=id=>document.getElementById(id);
async function post(u,d){const r=await fetch(u,{method:'POST',body:JSON.stringify(d||{})});return r.json()}
async function add(){const paths=$('paths').value.split('\n').map(s=>s.trim()).filter(Boolean);
 $('msg').textContent='copying...';const r=await post('/api/add',{paths,credit:$('credit').value});
 $('msg').textContent=r.error?r.error:'queued: '+r.added.join(', ');if(!r.error)$('paths').value='';tick()}
document.body.addEventListener('dragover',e=>e.preventDefault());
document.body.addEventListener('drop',e=>{e.preventDefault();const t=e.dataTransfer.getData('text');if(t)$('paths').value+=t+'\n'});
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){try{const d=await (await fetch('/api/jobs')).json();
 $('w').textContent=d.watcher?'engine running':'engine stopped';
 let h=d.waiting.map(n=>`<div class="job">${esc(n)}<div class="mut">waiting to be picked up...</div></div>`).join('');
 h+=d.jobs.map(j=>`<div class="job"><b>${esc(j.name)}</b> <span class="mut">#${j.id} ${esc(j.credit||'')}</span>
 <div class="bar"><i style="width:${j.pct}%"></i></div><span class="mut">${esc(j.status)} - ${esc(j.label)}</span>
 ${j.status=='done'?` <button class="s" onclick="post('/api/open',{path:${JSON.stringify(j.output).replace(/"/g,'&quot;')}})">Open folder</button>
 <button onclick="send(${JSON.stringify(j.output).replace(/"/g,'&quot;')})">${j.sent?'Resend remaining to':'Send to'} TikTok inbox</button>`:''}
 ${j.tiktok?`<div class="${j.tiktok.state=='error'?'err':'mut'}">${esc(j.tiktok.label)}</div>`:''}
 ${j.error?`<div class="err">${esc(j.error)}</div>`:''}</div>`).join('');
 $('jobs').innerHTML=h||'<span class="mut">no jobs yet</span>';
 if(!$('log').hidden)showLog(true)}catch(e){}}
async function showLog(keep){$('log').hidden=false;$('log').textContent=(await (await fetch('/api/log')).json()).log}
const F={interval:['Part length (s)','number'],model:['Whisper model','text'],language:['Language ("" = auto)','text'],device:['Device (cpu/cuda)','text'],fit:['Fit (blur/crop)','text'],credit:['Default credit','text']};
async function loadSet(){const s=await (await fetch('/api/settings')).json();
 $('set').innerHTML=Object.keys(F).map(k=>`<div><label>${F[k][0]}</label><br><input id="s_${k}" type="${F[k][1]}" value="${esc(s[k])}"></div>`).join('')}
async function save(){const d={};Object.keys(F).forEach(k=>d[k]=$('s_'+k).value);const r=await post('/api/settings',d);$('smsg').textContent=r.error||'saved'}
async function tstat(){const t=await (await fetch('/api/tiktok')).json();
 $('tstat').textContent=t.connected?'- connected':(t.has_keys?'- keys saved, not connected':'- not set up');if(t.client_key&&!$('ck').value)$('ck').value=t.client_key}
async function saveKeys(){await post('/api/tiktok/keys',{client_key:$('ck').value,client_secret:$('cs').value});$('cs').value='';tstat()}
async function connect(){const r=await post('/api/tiktok/connect');$('tmsg').textContent=r.error||'';if(!r.error)$('tmsg').textContent='Finish the login in your browser, then wait...';setTimeout(tstat,15000)}
async function send(p){const r=await post('/api/tiktok/send',{path:p});if(r.error)alert(r.error);tick()}
setInterval(tstat,10000);tstat();
loadSet();tick();setInterval(tick,3000);
</script></body></html>"""


def main():
    args = sys.argv[1:]
    if args:
        try:
            print("queued:", queue_video(args[0], args[1] if len(args) > 1 else ""))
        except Exception as e:
            print("ERROR:", e)
            input("press Enter...")
            return
    start_watcher()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:  # UI already running -> just open it
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return
    url = f"http://127.0.0.1:{PORT}"
    edge = shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if Path(edge).exists():
        subprocess.Popen([edge, f"--app={url}"], creationflags=NO_WINDOW)
    else:
        webbrowser.open(url)
    srv.serve_forever()


if __name__ == "__main__":
    main()

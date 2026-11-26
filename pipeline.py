#!/usr/bin/env python3
"""
pipeline.py - free, fully local TikTok clip maker.

Drop a video into  D:\\TikTok\\01_inbox  (optionally in a sub-folder named after the
creator, e.g. 01_inbox\\@somecreator\\video.mp4). The watcher then:

  1. waits until the file has finished downloading/copying
  2. transcribes the audio locally with Whisper (free, offline after first run)
  3. splits the video every N minutes (cut moved to the nearest sentence end)
  4. renders each part as a 1080x1920 TikTok video with burned-in captions,
     a "Part x/y" label and the creator credit
  5. puts the parts + a ready-to-paste caption text in 03_output\\<video name>\\

Nothing here costs money: Python, FFmpeg and faster-whisper are free and run on your PC.

Commands:
  python pipeline.py run          # watch the inbox forever (what the background task runs)
  python pipeline.py once         # process what's in the inbox now, then exit
  python pipeline.py status       # list processed videos
  python pipeline.py retry        # re-process videos in 99_failed
"""

import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import time
import tomllib
import traceback
from datetime import datetime
from pathlib import Path

import captions as cap

HERE = Path(__file__).resolve().parent
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
PARTIAL_EXT = {".part", ".crdownload", ".tmp", ".download", ".partial"}

log = logging.getLogger("tiktok")


# ============================================================== config, logging, state
def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    root = Path(cfg["paths"]["root"])
    cfg["_dirs"] = {k: root / v for k, v in {
        "inbox": "01_inbox", "processing": "02_processing", "output": "03_output",
        "failed": "99_failed", "logs": "logs"}.items()}
    for d in cfg["_dirs"].values():
        d.mkdir(parents=True, exist_ok=True)
    cfg["_db"] = root / "pipeline.db"
    return cfg


def setup_logging(cfg):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(cfg["_dirs"]["logs"] / "pipeline.log",
                                              maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if (sys.stdout and sys.stdout.isatty()) or os.environ.get("TIKTOK_CONSOLE_LOG"):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def db_connect(cfg) -> sqlite3.Connection:
    con = sqlite3.connect(cfg["_db"])
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY, name TEXT, source TEXT, credit TEXT, meta TEXT,
        status TEXT, parts INTEGER, output TEXT, error TEXT, created TEXT, updated TEXT)""")
    return con


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def safe(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name)[:60].strip("_") or "video"


# ============================================================== 1. inbox watcher
_seen: dict[str, tuple] = {}


def is_stable(p: Path, stable_seconds: int) -> bool:
    """True when the file hasn't been touched for stable_seconds and isn't locked."""
    st = p.stat()
    if st.st_size == 0 or time.time() - st.st_mtime < stable_seconds:
        return False
    sig = (st.st_size, st.st_mtime)
    prev = _seen.get(str(p))
    _seen[str(p)] = sig
    if prev and prev != sig:        # changed since last poll -> still being written
        return False
    try:                            # Windows locks files that are still being copied
        with open(p, "ab"):
            pass
    except OSError:
        return False
    return True


def scan_inbox(cfg, con):
    inbox = cfg["_dirs"]["inbox"]
    for p in sorted(inbox.rglob("*")):
        if (not p.is_file() or p.suffix.lower() in PARTIAL_EXT
                or p.suffix.lower() not in VIDEO_EXT):
            continue
        if not is_stable(p, cfg["watch"].get("stable_seconds", 15)):
            continue
        _seen.pop(str(p), None)

        # credit: sidecar <video>.json  >  creator sub-folder name  >  default
        meta = {}
        sidecar = p.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"Ignoring bad sidecar {sidecar.name}: {e}")
        rel = p.relative_to(inbox)
        folder_credit = rel.parts[0] if len(rel.parts) > 1 else None
        credit = meta.get("credit") or folder_credit or cfg["text"].get("default_credit", "")

        cur = con.execute("INSERT INTO jobs(name, source, credit, meta, status, created, updated) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (p.stem, "", credit, json.dumps(meta, ensure_ascii=False), "new",
                           now_iso(), now_iso()))
        job_id = cur.lastrowid
        jdir = cfg["_dirs"]["processing"] / f"{job_id:04d}_{safe(p.stem)}"
        jdir.mkdir(parents=True, exist_ok=True)
        dest = jdir / f"source{p.suffix.lower()}"
        shutil.move(str(p), dest)
        words_file = p.with_suffix(".words.json")
        if sidecar.exists():
            shutil.move(str(sidecar), jdir / "meta.json")
        if words_file.exists():
            shutil.move(str(words_file), jdir / "words.json")
        con.execute("UPDATE jobs SET source=? WHERE id=?", (str(dest), job_id))
        con.commit()
        log.info(f"[{job_id}] picked up {rel}  (credit: {credit or '-'})")


# ============================================================== 2. splitting
def split_by_interval(words, duration, sp) -> list[tuple[float, float]]:
    """
    Cut every `interval_seconds`. If snap_to_sentence is on, each cut moves to the
    best sentence end / pause within +-snap_window_seconds, so no word is cut in half.
    """
    interval = float(sp["interval_seconds"])
    window = float(sp.get("snap_window_seconds", 30))
    min_last = float(sp.get("min_last_part_seconds", 60))

    if duration <= interval + min_last:
        return [(0.0, duration)]

    # candidate cut points between consecutive words
    cands = []
    for a, b in zip(words, words[1:]):
        cands.append({"t": (a["end"] + b["start"]) / 2,
                      "gap": b["start"] - a["end"],
                      "sent": a["word"][-1:] in ".!?"})

    cuts, s = [], 0.0
    while duration - s > interval + min_last:
        target = s + interval
        cut = target
        if sp.get("snap_to_sentence", True):
            near = [c for c in cands if abs(c["t"] - target) <= window and c["t"] - s > 10]
            if near:
                def score(c):
                    return ((10 if c["sent"] else 0) + min(c["gap"], 2.0) * 5
                            - abs(c["t"] - target) / window * 4)
                cut = max(near, key=score)["t"]
            else:
                # no speech near the target (music/silence): cut in the middle of the
                # longest silence in the window, or exactly at the target
                gaps = [c for c in cands if abs(c["t"] - target) <= window * 2 and c["t"] - s > 10]
                if gaps:
                    cut = max(gaps, key=lambda c: c["gap"])["t"]
        cuts.append(cut)
        s = cut

    bounds = [0.0] + cuts + [duration]
    return list(zip(bounds, bounds[1:]))


def video_title(fmt: str, name: str, part: int, total: int) -> str:
    """Text burned on top of each part, e.g. 'my video Part 2'. Single-part videos show just the name."""
    if " " not in name:                         # 'Rick.and.Morty.S09E01' -> 'Rick and Morty S09E01'
        name = re.sub(r"[._]+", " ", name).strip()
    if total <= 1:
        return name
    return fmt.format(name=name, part=part, total=total)


# ============================================================== 3. caption text for TikTok
def build_post_text(cfg, job, meta, i, total) -> str:
    t = cfg["text"]
    title = meta.get("title") or t.get("title_format", "{name}").format(name=job["name"])
    if total > 1:
        title = f"{title} (Part {i}/{total})"
    parts = [title]
    if job["credit"] and t.get("credit_caption_format", "🎥 Credit: {credit}"):
        parts.append(t["credit_caption_format"].format(credit=job["credit"]))
    if t.get("footer"):
        parts.append(t["footer"])
    tags = list(dict.fromkeys(meta.get("hashtags", []) + t.get("hashtags", [])))
    if tags:
        parts.append(" ".join(x if x.startswith("#") else f"#{x}" for x in tags))
    return "\n\n".join(parts)[:2200]


# ============================================================== processing
def process_job(cfg, con, job):
    jdir = Path(job["source"]).parent
    source = Path(job["source"])
    meta = json.loads(job["meta"] or "{}")
    jid = job["id"]
    t_start = time.time()
    con.execute("UPDATE jobs SET status='processing', updated=? WHERE id=?", (now_iso(), jid))
    con.commit()

    duration = cap.probe_duration(source)
    log.info(f"[{jid}] processing '{job['name']}' ({duration / 60:.1f} min)")

    # --- transcription (cached in words.json so a crash/retry doesn't redo it)
    words_path = jdir / "words.json"
    want_captions = cfg["captions"].get("enabled", True)
    if words_path.exists():
        words = json.loads(words_path.read_text(encoding="utf-8"))
        log.info(f"[{jid}] using existing transcript ({len(words)} words)")
    elif want_captions or cfg["split"].get("snap_to_sentence", True):
        t0 = time.time()
        wh = cfg["whisper"]
        words = cap.transcribe(source, wh.get("model", "small"),
                               wh.get("language") or meta.get("language"),
                               log=lambda m: log.info(f"[{jid}] {m}"),
                               device=wh.get("device", "cpu"))
        words_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        log.info(f"[{jid}] transcribed {len(words)} words in {(time.time() - t0) / 60:.1f} min")
    else:
        words = []

    # --- split
    segs = split_by_interval(words, duration, cfg["split"])
    total = len(segs)
    log.info(f"[{jid}] -> {total} part(s): " + ", ".join(f"{(e - s) / 60:.1f}m" for s, e in segs))

    # --- render parts into 03_output\<name>\
    out_dir = cfg["_dirs"]["output"] / f"{jid:04d}_{safe(job['name'])}"
    out_dir.mkdir(parents=True, exist_ok=True)
    style, ad = cfg["captions"], cfg.get("ad", {})
    ad = ad if ad.get("enabled") else None
    fmt = cfg["text"].get("credit_on_video_format", "Credit: {credit}")
    credit_on_video = fmt.format(credit=job["credit"]) if job["credit"] and fmt else None
    post_texts = []

    for i, (s, e) in enumerate(segs, 1):
        clip_words = cap.slice_words(words, s, e) if want_captions else []
        chunks = cap.chunk_words(clip_words, style.get("max_words", 4),
                                 style.get("max_chars", 22), style.get("max_dur", 2.2))
        label = (video_title(style.get("title_format", "{name} Part {part}"), job["name"], i, total)
                 if style.get("part_label", True) else None)
        ass = jdir / f"part{i:02d}.ass"
        ass.write_text(cap.build_ass(chunks, e - s, style, label, credit_on_video, ad), encoding="utf-8")

        out = out_dir / f"part{i:02d}.mp4"
        t0 = time.time()
        log.info(f"[{jid}] rendering part {i}/{total} ({(e - s) / 60:.1f} min)")
        cap.render_clip(source, s, e, ass, out, cfg["video"].get("fit", "blur"),
                        cfg["video"].get("preset", "medium"), cfg["video"].get("crf", 20))
        (out_dir / f"part{i:02d}.srt").write_text(cap.build_srt(chunks), encoding="utf-8")
        text = build_post_text(cfg, job, meta, i, total)
        (out_dir / f"part{i:02d}.txt").write_text(text, encoding="utf-8")
        post_texts.append(f"===== part{i:02d}.mp4 =====\n{text}\n")
        log.info(f"[{jid}] part {i}/{total} done in {time.time() - t0:.0f}s")

    (out_dir / "_captions_to_paste.txt").write_text("\n".join(post_texts), encoding="utf-8")

    # --- tidy up: keep the original + transcript next to the parts (or delete)
    src_dir = out_dir / "_source"
    if cfg["paths"].get("keep_source", True):
        src_dir.mkdir(exist_ok=True)
        shutil.move(str(source), src_dir / source.name)
    else:
        source.unlink(missing_ok=True)
    if words_path.exists():
        src_dir.mkdir(exist_ok=True)
        shutil.move(str(words_path), src_dir / "words.json")
    shutil.rmtree(jdir, ignore_errors=True)

    con.execute("UPDATE jobs SET status='done', parts=?, output=?, source=?, updated=? WHERE id=?",
                (total, str(out_dir), str(src_dir / source.name), now_iso(), jid))
    con.commit()
    log.info(f"[{jid}] DONE: {total} part(s) in {out_dir}  ({(time.time() - t_start) / 60:.1f} min total)")
    if cfg["watch"].get("open_folder_when_done", True) and os.name == "nt":
        try:
            os.startfile(out_dir)  # opens Explorer on the finished parts
        except Exception:
            pass


def fail_job(cfg, con, job, err: str):
    jdir = Path(job["source"]).parent
    dest = cfg["_dirs"]["failed"] / jdir.name
    try:
        if jdir.exists():
            shutil.move(str(jdir), dest)
            (dest / "error.txt").write_text(err, encoding="utf-8")
    except Exception as e:
        log.error(f"could not move failed job: {e}")
    con.execute("UPDATE jobs SET status='failed', error=?, source=?, updated=? WHERE id=?",
                (err[-2000:], str(dest / Path(job["source"]).name), now_iso(), job["id"]))
    con.commit()


# ============================================================== main
def one_pass(cfg, con):
    scan_inbox(cfg, con)
    for job in con.execute("SELECT * FROM jobs WHERE status='new' ORDER BY id").fetchall():
        try:
            process_job(cfg, con, job)
        except Exception:
            err = traceback.format_exc()
            log.error(f"[{job['id']}] FAILED:\n{err}")
            fail_job(cfg, con, job, err)


def cmd_status(con):
    rows = con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        print("nothing processed yet")
    for j in rows:
        extra = f"{j['parts']} part(s) -> {j['output']}" if j["status"] == "done" else (j["error"] or "")[-80:]
        print(f"{j['id']:>4}  {j['status']:<10} {j['name'][:40]:<40} {extra}")


def cmd_retry(cfg, con):
    n = 0
    for j in con.execute("SELECT * FROM jobs WHERE status='failed'").fetchall():
        src = Path(j["source"])
        if src.exists():
            back = cfg["_dirs"]["processing"] / src.parent.name
            shutil.move(str(src.parent), back)
            (back / "error.txt").unlink(missing_ok=True)
            con.execute("UPDATE jobs SET status='new', error=NULL, source=? WHERE id=?",
                        (str(back / src.name), j["id"]))
            n += 1
    con.commit()
    print(f"re-queued {n} video(s); the watcher will pick them up")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = load_config(Path(os.environ.get("TIKTOK_CONFIG", HERE / "config.toml")))
    setup_logging(cfg)
    con = db_connect(cfg)
    con.execute("UPDATE jobs SET status='new' WHERE status='processing'")  # resume after crash/reboot
    con.commit()

    if cmd == "status":
        return cmd_status(con)
    if cmd == "retry":
        return cmd_retry(cfg, con)
    if cmd == "once":
        return one_pass(cfg, con)

    (cfg["_dirs"]["inbox"].parent / "watcher.pid").write_text(str(os.getpid()))  # lets the UI find this process
    log.info(f"Watching {cfg['_dirs']['inbox']}")
    while True:
        try:
            one_pass(cfg, con)
        except Exception:
            log.error("loop error:\n" + traceback.format_exc())
        time.sleep(cfg["watch"].get("poll_seconds", 10))


if __name__ == "__main__":
    main()

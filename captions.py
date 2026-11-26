"""
captions.py - transcription, caption styling and rendering for TikTok clips.

Used by pipeline.py. Everything here is pure functions so it can also be
reused from other scripts.
"""

import os
import subprocess
from pathlib import Path

W, H = 1080, 1920


# ---------------------------------------------------------------- transcription
_MODEL_CACHE = {}


def transcribe(video: Path, model_name: str = "small", language: str | None = None, log=print,
               device: str = "cpu") -> list[dict]:
    """Return [{"word", "start", "end"}] for the whole video using faster-whisper."""
    from faster_whisper import WhisperModel

    if model_name not in _MODEL_CACHE:
        log(f"Loading Whisper model '{model_name}' on {device} (first run downloads it)...")
        compute = "float16" if device == "cuda" else "int8"
        _MODEL_CACHE[model_name] = WhisperModel(model_name, device=device, compute_type=compute)
    model = _MODEL_CACHE[model_name]

    segments, info = model.transcribe(str(video), language=language or None,
                                      word_timestamps=True, vad_filter=True)
    log(f"Detected language: {info.language} ({info.language_probability:.0%})")
    words = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append({"word": t, "start": round(w.start, 3), "end": round(w.end, 3)})
    return words


def slice_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Words inside [start, end], re-timed so the clip starts at 0."""
    out = []
    for w in words:
        if w["start"] >= start - 0.05 and w["end"] <= end + 0.05:
            out.append({"word": w["word"],
                        "start": round(max(0.0, w["start"] - start), 3),
                        "end": round(max(0.0, w["end"] - start), 3)})
    return out


# ---------------------------------------------------------------- caption chunks
def chunk_words(words, max_words=4, max_chars=22, max_dur=2.2):
    chunks, cur = [], []

    def flush():
        if cur:
            chunks.append({"text": " ".join(w["word"] for w in cur),
                           "start": cur[0]["start"], "end": cur[-1]["end"], "words": list(cur)})
            cur.clear()

    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            new_len = len(" ".join(x["word"] for x in cur)) + 1 + len(w["word"])
            if (len(cur) >= max_words or new_len > max_chars
                    or w["end"] - cur[0]["start"] > max_dur or gap > 0.6):
                flush()
        cur.append(w)
        if w["word"][-1:] in ".!?,;:":
            flush()
    flush()
    for a, b in zip(chunks, chunks[1:]):
        if b["start"] - a["end"] < 0.4:
            a["end"] = b["start"]
    return chunks


# ---------------------------------------------------------------- subtitle files
def _ts_ass(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ts_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _esc(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def build_ass(chunks, duration, style: dict, part_label: str | None = None,
              credit: str | None = None, ad: dict | None = None) -> str:
    font = style.get("font", "Arial")
    size = style.get("font_size", 86)
    margin = style.get("margin_bottom", 520)
    upper = style.get("uppercase", True)
    hl = style.get("highlight", "00E5FF")
    ad_color = (ad or {}).get("color", "3B2BFE")
    # title on top: no black box by default (just outlined lettering), optionally see-through
    alpha = round((1 - min(max(float(style.get("title_opacity", 0.85)), 0.0), 1.0)) * 255)
    title_fill = f"&H{alpha:02X}FFFFFF"
    title_border = "3,16,0" if style.get("title_box", False) else "1,3,2"   # BorderStyle,Outline,Shadow

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,80,80,{margin},1
Style: Part,{font},{style.get("title_size", 52)},{title_fill},&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,{title_border},8,70,70,170,1
Style: Credit,{font},38,&H40FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,70,70,380,1
Style: Ad,{font},62,&H00FFFFFF,&H00FFFFFF,&H00{ad_color},&H00{ad_color},-1,0,0,0,100,100,0,0,3,22,0,8,70,70,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for c in chunks:
        ws = c["words"]
        if hl and len(ws) > 1:
            for i in range(len(ws)):
                start = ws[i]["start"] if i else c["start"]
                end = ws[i + 1]["start"] if i + 1 < len(ws) else c["end"]
                if end <= start:
                    continue
                parts = []
                for j, x in enumerate(ws):
                    t = _esc(x["word"].upper() if upper else x["word"])
                    parts.append(f"{{\\c&H{hl}&}}{t}{{\\c&HFFFFFF&}}" if j == i else t)
                ev.append(f"Dialogue: 1,{_ts_ass(start)},{_ts_ass(end)},Caption,,0,0,0,,{' '.join(parts)}")
        else:
            t = c["text"].upper() if upper else c["text"]
            ev.append(f"Dialogue: 1,{_ts_ass(c['start'])},{_ts_ass(c['end'])},Caption,,0,0,0,,{_esc(t)}")

    if part_label:
        secs = float(style.get("label_seconds", 0))          # 0 = keep the title on screen the whole clip
        end = min(secs, duration) if secs > 0 else duration
        ev.append(f"Dialogue: 2,{_ts_ass(0)},{_ts_ass(end)},Part,,0,0,0,,"
                  f"{{\\fad(0,300)}}{_esc(part_label)}")
    if credit:
        ev.append(f"Dialogue: 0,{_ts_ass(0)},{_ts_ass(duration)},Credit,,0,0,0,,{_esc(credit)}")
    if ad and ad.get("text"):
        mode, secs = ad.get("mode", "end"), float(ad.get("seconds", 4))
        a0, a1 = {"full": (0.0, duration),
                  "start": (3.0, min(3.0 + secs, duration)),   # after the Part label
                  }.get(mode, (max(0.0, duration - secs), duration))
        ev.append(f"Dialogue: 3,{_ts_ass(a0)},{_ts_ass(a1)},Ad,,0,0,0,,{{\\fad(250,0)}}{_esc(ad['text'])}")
    return header + "\n".join(ev) + "\n"


def build_srt(chunks) -> str:
    return "\n".join(f"{i}\n{_ts_srt(c['start'])} --> {_ts_srt(c['end'])}\n{c['text']}\n"
                     for i, c in enumerate(chunks, 1))


# ---------------------------------------------------------------- render
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console pop-up per ffmpeg call


def probe_duration(video: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(video)],
                       capture_output=True, text=True, check=True, creationflags=NO_WINDOW)
    return float(r.stdout.strip())


def _audio_filter(source: Path) -> str:
    """Stereo downmix that keeps dialogue (centre channel) loud, then level to TikTok-friendly loudness.
    5.1 files exported as-is lose their voices on phones, where the centre channel is dropped."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=channels", "-of", "default=nw=1:nk=1", str(source)],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        ch = int(r.stdout.strip() or 2)
    except Exception:
        ch = 2
    norm = "loudnorm=I=-16:TP=-1.5:LRA=11"
    if ch == 6:      # FL FR FC LFE SL/BL SR/BR
        return "pan=stereo|FL=0.40*c0+0.85*c2+0.25*c4|FR=0.40*c1+0.85*c2+0.25*c5," + norm
    return norm


def render_clip(source: Path, start: float, end: float, ass_path: Path, out: Path, fit: str = "blur",
                preset: str = "medium", crf: int = 20):
    """Cut [start, end] from source, reframe to 1080x1920, burn in the .ass file."""
    if fit == "crop":
        frame = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        # the background is blurred at 1/4 size and scaled back up: looks the same, renders ~2x faster
        frame = (f"split[a][b];[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,"
                 f"crop={W // 4}:{H // 4},boxblur=6:2,scale={W}:{H}:flags=bilinear[bg];"
                 f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    # bare .ass filename + cwd avoids the Windows drive-letter escaping problem
    vf = f"{frame},setsar=1,subtitles={ass_path.name}[v]"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source.resolve()),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?", "-af", _audio_filter(source),
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-r", "30",
           "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "44100", "-movflags", "+faststart",
           str(out.resolve())]
    subprocess.run(cmd, check=True, cwd=ass_path.parent, creationflags=NO_WINDOW)

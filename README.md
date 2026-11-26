# TikTok Clip Maker

A local Windows tool that turns a long video into vertical, captioned, numbered parts and sends them
to your TikTok inbox. It uses only free tools; video processing and transcription run on your own PC.

## What it does

- Takes a video by path (or dropped into a watch folder)
- Transcribes speech with word-level timestamps (faster-whisper)
- Splits into parts of a set length, optionally snapping cuts to sentence ends
- Renders 1080x1920 with a blurred background, word-highlighted captions and a "<name> Part n/N" title
- Writes a ready-to-paste caption for each part
- Shows the job queue and progress in a local web UI
- Sends finished parts to your TikTok inbox via the Content Posting API (OAuth with PKCE, chunked upload)

## Stack

Python 3.11+ (standard library web server), FFmpeg, faster-whisper, SQLite, TikTok Content Posting API.

| File | Role |
|---|---|
| `pipeline.py` | watcher and job processing |
| `captions.py` | transcription, caption files, FFmpeg rendering |
| `app.py` | local web UI on `127.0.0.1:8765` |
| `tiktok_upload.py` | TikTok login and upload |
| `config.toml` | all settings |

## Problems solved along the way

- **Voices missing on phones:** 5.1 source audio was passed through. It is now downmixed to stereo with a
  stronger centre channel and loudness normalisation.
- **Slow rendering:** measurement showed the background blur was the bottleneck. Blurring at quarter size
  made rendering about 2x faster.
- **Large uploads rejected:** fixed the chunk sizes sent to TikTok.

## Setup

1. Install Python 3.11 or newer.
2. Run `powershell -ExecutionPolicy Bypass -File .\install.ps1` (installs FFmpeg and the Python packages,
   downloads the speech model and registers a background task).
3. Start `ClipMaker.bat`, or drag a video file onto it.

Folders are created under `D:\TikTok` by default; change `[paths] root` in `config.toml`.

### TikTok upload (optional)

Create an app at developers.tiktok.com with Login Kit and Content Posting API, set the redirect URI to
`http://127.0.0.1:8765/tiktok/callback`, then enter your Client key and secret in the UI. Keys and tokens are
stored in `<root>\tiktok_auth.json`, outside this repository. TikTok delivers each video to your inbox; you add
the caption and publish it in the app.

## Built with AI

This project was developed with [Claude Code](https://claude.com/claude-code). I defined the requirements,
tested the results on PC and phone, reported problems and made the decisions; the AI wrote the code,
diagnosed bugs and helped set up the TikTok integration.

## Rights

Use only videos you own or have permission to publish.

# TOS: Autonomous YouTube Shorts Pipeline

TOS ("Tam Otonom Sistem", Turkish for *fully autonomous system*) takes a topic and produces a finished vertical short. It writes the script, generates the narration, aligns subtitles word by word, finds a matching licensed visual for every sentence, renders the video and uploads it to YouTube. One command per video, or a daily schedule across several channels.

The content language is Turkish. The architecture is language-agnostic: swap the TTS voice, the Whisper language and the master prompt to change it.

## How it works

```
topic queue ──► brain.py ──► scenario JSON ──► produce.py ──► .mp4 + thumbnail + meta ──► upload.py ──► YouTube
 (per channel)   LLM writer   (beats, hook,      orchestrates the       (quality gate first)      OAuth, per-channel
                              queries, meta)     micro-services                                     token
```

`run.py` is the orchestrator. `daily.py` runs every channel's daily quota and is meant for Windows Task Scheduler.

### 1. Script writer (`brain.py`, `MASTER_PROMPT.md`)
The LLM returns a strict JSON scenario instead of free text:

- a **hook** overlay for the first 1.5 s
- the narration split into **10–14 beats**. Each beat is `{text, query, kind}`: a short sentence, an English visual search query for that exact moment, and whether the beat needs a *specific* image (a named person, place or event) or a *generic* mood shot
- an **anchor**: the topic's canonical English name, used to keep specific beats on topic
- `visual_mode` (`photo` for archival/historical topics, `video` for atmospheric/tech topics), title, description, on-screen labels

The default backend is Gemini, with a model fallback chain, retry/backoff and DNS-failure handling. It also repairs malformed JSON: a second object appended to the first, unescaped quotes, raw newlines inside strings. Without an API key it falls back to a local Ollama model. Channel profiles in `channels.json` inject a per-niche style guide into the prompt.

### 2. Media micro-services (Flask, local)

| Service | Port | Job |
|---|---|---|
| `edge_tts_server.py` | 5002 | Neural TTS (Edge-TTS), trims long pauses *before* alignment so sync is preserved, voice fallback chain |
| `subtitle_server.py` | 5060 | Whisper forced alignment (stable-ts) → word-level ASS subtitles, plus the time range of every beat |
| `photo_server.py` | 5080 | Searches Wikimedia Commons, Openverse, The Met, Rijksmuseum, Pixabay and Pexels for photos or video clips, one per beat |
| `ffmpeg_server.py` | 5050 | Renders 9:16 video: Ken Burns on photos or cut clips, per-scene durations, hook overlay, subtitles, HUD layer, music bed, thumbnail |
| `piper_server.py` | 5000 | Offline TTS fallback (optional) |
| `clip_rank_server.py` | 5004 | CLIP re-ranker for candidate images (optional; the pipeline keeps the original order when it is down) |

### 3. Beat-synced visuals
Splitting a video's length evenly across images puts the wrong image on screen. Here every beat gets its own query, Whisper returns when each beat is actually spoken, and the renderer shows each visual for exactly that span. Very short and very long scenes are smoothed by borrowing time from neighbours, and the total length stays equal to the audio length.

### 4. Relevance scoring (`photo_server.py`)
Ranking rewards **relevance over format**:

- proper-noun match ≫ year match ≫ generic keyword match; the orientation bonus is small
- stock sites (Pixabay/Pexels) have tag-bag titles, so they only get proper-noun credit when *all* names in the query match
- *specific* beats must also match the topic anchor. This stops "SS Mont-Blanc" from returning the mountain.
- *generic* beats need at least two signals
- irrelevant candidates are **dropped**, not just ranked lower. An empty beat reuses a relevant neighbour.
- cross-beat de-duplication, and a per-topic archive pool when a long query returns nothing
- license filter: public domain / CC0 / CC-BY only

Each video is written with a `_beats.txt` report: the beat text, the query, the chosen image and its relevance score. Problem beats are flagged, so quality can be audited without watching the video.

### 5. Guard rails
- **Quality gate** (`run.py`): a video is not uploaded if too few beats matched their query or too few distinct visuals were used (`SHORTS_MIN_ALAKA`, `SHORTS_MIN_FARKLI`)
- **Health check** (`saglik.py`): before a scheduled run, every dependency is asked to do a small *real* job (synthesize speech, align it, fetch an image, get a short reply from the LLM, check ffmpeg and API keys), not just answer on its port. A failing layer stops the run with a reason.
- **Shorts length guard**: if the slowed-down documentary narration pushes past 58 s, the audio is re-synthesized at normal speed
- uploads default to `unlisted`

## Stack
Python · Flask · Gemini API / Ollama · Edge-TTS · Whisper (stable-ts) · FFmpeg + libass · Wikimedia Commons / Openverse / Met / Rijksmuseum / Pixabay / Pexels APIs · YouTube Data API v3 · optional CLIP (transformers)

## Setup

Requirements: Python 3.10+, `ffmpeg` on PATH. The batch scripts are Windows; on other systems start the services by hand.

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in GEMINI_API_KEY, PIXABAY_API_KEY, CONTACT_EMAIL
```

Optional background music: put a track at `assets/music/cinematic1.mp3`. Without it, videos render without a music bed.

YouTube upload (only needed for non-dry runs):
1. Google Cloud Console → enable **YouTube Data API v3**
2. Create an **OAuth client ID (Desktop app)**, download it as `client_secret.json` into the repo root
3. On the first upload a browser opens; the token is saved as `token_<channel>.json`

Both files are git-ignored.

## Usage

```bash
start_all.bat                          # start the services (Windows)
python saglik.py                       # health check

python run.py kanal1 --fill 10         # generate 10 topics for the channel's niche into its queue
python run.py kanal1 --dry             # produce one video, no upload
python run.py kanal1                   # produce + upload (unlisted)
python run.py kanal1 --topic "..."     # specific topic
python run.py kanal1 --batch 3         # three in a row

python auto.py "<topic>"               # topic → video, no queue, no upload
python produce.py scenario.json        # render an existing scenario
python daily.py                        # every channel's daily quota (schedule daily.bat)
```

Channels are defined in `channels.json` (niche, voice, privacy, videos per day, style guide).

## Content policy built into the prompt
Topics that need copyrighted footage (TV series, films, celebrities) are rejected at the script stage. No free-licensed visuals exist for them, and reusing clips would risk copyright strikes. The system is designed for history, science, technology and psychology topics, where archival and CC imagery exists.

# ytsum

Turn a YouTube video into a Markdown brief you can read in a minute.

```sh
./ytsum.py "https://www.youtube.com/watch?v=..."          # local model, free
./ytsum.py URL1 URL2 URL3                                  # several in one run
./ytsum.py URL --engine claude                             # the Claude CLI instead
./ytsum.py URL --model gemma4:latest                       # a smaller local model
./ytsum.py URL --comments 0                                # skip the comment pass
```

The brief lands in `out/YYYY-MM-DD-channel-title.md` with five sections: the point, key
points with timestamps, the facts and numbers the video states, what it means for you, and
what the video claims without evidence. A sixth section holds the comments that carry
information. Each fact carries its own context, in the form
"90 fps: the frame rate in most areas", because a reader sees the line alone.

## The web page

`ytsum_ui.py` serves a local page at `http://127.0.0.1:8765` and opens it in the browser.
You paste one or more YouTube links into the box, and the page runs `ytsum.py` once for each link.
The runs go one at a time, because the local model uses the whole GPU.
The page lists the briefs in `out/`. When a brief is ready, the page shows it.

```sh
./ytsum_ui.py                  # start the page
./ytsum_ui.py --no-browser     # start the server only
```

The page uses the standard library only. It listens on 127.0.0.1, so no other machine can reach it.
Set `YTSUM_PORT` to use a port other than 8765.
If the server already runs, a second start opens the page and stops.
On macOS, you can double-click `ytsum-ui.command` in Finder to start the page.

### Set up on Windows

1. Install Python 3.11 or later, ffmpeg (`winget install ffmpeg`), and Ollama with the default model.
2. Make a virtual environment in the repository, and install yt-dlp into it:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -U "yt-dlp[default]"
   ```

3. Start the page with `.venv\Scripts\pythonw ytsum_ui.py`. `pythonw` opens no console window.

The page puts `.venv\Scripts` first on the PATH of each run, so `ytsum.py` finds that yt-dlp.
If YouTube changes and yt-dlp fails, run the `pip install -U` command again.

## How it works

1. `yt-dlp` reads the metadata and the English caption track. It downloads no video.
2. The parser removes the caption duplicates. Auto-captions scroll a two-line window, so
   each block repeats the line before it, and a raw transcript says each line two times.
3. Every 30 seconds the transcript keeps a `[mm:ss]` stamp, so a bullet can cite the moment.
4. A model writes the brief. A transcript longer than 7,000 words goes in parts, and one
   more call merges the parts into a single brief.
5. `yt-dlp` reads the 60 most liked comments. A second model call keeps the comments that
   support a claim with a reason, add information the video leaves out, or dispute a claim
   and say why. Each line carries the tag `[Supports]`, `[Adds]` or `[Disputes]` and the
   like count. `--comments 0` skips the pass, and `--comments 200` reads more.
6. The tool copies each link from the video description to a "Links from the description"
   list, with the text on the same line as the label. This gives you the mod page, the
   product page, or the repository. A description with no link gets no list.

The prompt holds the Simple Technical English rules, so the bullets read the same way every
time: one idea per sentence, active voice, plain words, and the number instead of "a lot".

## Requirements

- `yt-dlp` (`brew install yt-dlp`).
- `ffmpeg` (`brew install ffmpeg`). yt-dlp uses it to convert the captions to SRT.
- Ollama with one instruction model. The default is `gemma4:26b-a4b-it-qat`; set
  `YTSUM_MODEL` to change it. Or pass `--engine claude` and pay for the cloud call.
- `whisper-cli`, for the fallback only. A video with no captions makes the tool
  download the audio and transcribe it with whisper.cpp, and it says so in the brief. The
  default model file comes from the OpenSuperWhisper application. A 6-minute video takes 29
  seconds, and the word count agrees with the caption count to 2 percent.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `YTSUM_MODEL` | `gemma4:26b-a4b-it-qat` | the Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | the Ollama server |
| `YTSUM_OUT` | `./out` | where the briefs go |
| `YTSUM_WHISPER_MODEL` | `ggml-large-v3-turbo.bin` from OpenSuperWhisper | the whisper.cpp model file for the fallback |
| `YTSUM_INTEREST` | finance systems, payments, job search, hardware and AI tooling | what the "For me" section answers against |
| `YTSUM_PORT` | `8765` | the port of the web page |

Change `YTSUM_INTEREST` to change the lens. The rest of the prompt stays the same.

## To watch a channel

There is no scheduler here on purpose. `yt-dlp` already lists a channel's new videos, so one
line does the job, and `cron` or a `launchd` plist runs it:

```sh
yt-dlp --flat-playlist --print id --playlist-end 3 \
  "https://www.youtube.com/@GamersNexus/videos" \
  | while read id; do ./ytsum.py "https://youtu.be/$id"; done
```

Keep a file of the ids you have already summarised if you run it daily, or let `out/` be
that record: the filename holds the channel and the title.

## Checks

```sh
python3 test_ytsum.py
```

Offline: no network and no model. It tests the caption dedupe, the timestamps, the chunking
and the file the brief goes into.

# ytsum

Turn a YouTube video into a Markdown brief you can read in a minute.

```sh
./ytsum.py "https://www.youtube.com/watch?v=..."          # local model, free
./ytsum.py URL1 URL2 URL3                                  # several in one run
./ytsum.py URL --engine claude                             # the Claude CLI instead
./ytsum.py URL --model gemma4:latest                       # a smaller local model
```

The brief lands in `out/YYYY-MM-DD-channel-title.md` with five sections: the point, key
points with timestamps, the facts and numbers the video states, what it means for you, and
what the video claims without evidence. Each fact carries its own context, in the form
"90 fps: the frame rate in most areas", because a reader sees the line alone.

## How it works

1. `yt-dlp` reads the metadata and the English caption track. It downloads no video.
2. The parser removes the caption duplicates. Auto-captions scroll a two-line window, so
   each block repeats the line before it, and a raw transcript says each line two times.
3. Every 30 seconds the transcript keeps a `[mm:ss]` stamp, so a bullet can cite the moment.
4. A model writes the brief. A transcript longer than 7,000 words goes in parts, and one
   more call merges the parts into a single brief.
5. The tool copies each link from the video description to a "Links from the description"
   list, with the text on the same line as the label. This gives you the mod page, the
   product page, or the repository. A description with no link gets no list.

The prompt holds the Simple Technical English rules, so the bullets read the same way every
time: one idea per sentence, active voice, plain words, and the number instead of "a lot".

## Requirements

- `yt-dlp` (`brew install yt-dlp`).
- Ollama with one instruction model. The default is `gemma4:26b-a4b-it-qat`; set
  `YTSUM_MODEL` to change it. Or pass `--engine claude` and pay for the cloud call.
- `ffmpeg` and `whisper-cli`, for the fallback only. A video with no captions makes the tool
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

#!/usr/bin/env python3
"""Summarise a YouTube video into a Markdown brief.

    ./ytsum.py <url> [more urls...]

It pulls the caption track with yt-dlp, keeps a timestamp every half minute,
and asks a model for bullet points in Simple Technical English. The brief goes
to out/YYYY-MM-DD-channel-title.md.

Engines: --engine ollama (default, local and free) or claude (the CLI).
No captions on the video? It downloads the audio and uses whisper.cpp.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/generate"
MODEL = os.environ.get("YTSUM_MODEL", "gemma4:26b-a4b-it-qat")
OUT = Path(os.environ.get("YTSUM_OUT", Path(__file__).parent / "out"))
STAMP_EVERY = 30          # seconds between the timestamps kept in the transcript
WHISPER_MODEL = os.environ.get("YTSUM_WHISPER_MODEL", str(Path.home() /
    "Library/Application Support/ru.starmel.OpenSuperWhisper/whisper-models/ggml-large-v3-turbo.bin"))
CHUNK_WORDS = int(os.environ.get("YTSUM_CHUNK_WORDS", 3500))   # ~4,700 tokens, fits the 8k context Ollama loads by default

# ponytail: the rules live here as text, not as a config file or a fetched skill.
STYLE = """Write in Simple Technical English:
- One idea per sentence. 25 words maximum.
- Active voice. Name the actor.
- Simple present, simple past or simple future. No perfect tense.
- Plain words: use, not utilise. Before, not prior to. After, not subsequent to.
- No adjectives of praise, no marketing words, no irony.
- Give the number. "Wait 30 seconds", not "wait a moment".
Keep every technical name exactly as the speaker says it: a product, a part number, a version."""

PROMPT = """You read the transcript of a YouTube video. Write a brief for a reader who will not watch it.

{style}

Output this Markdown structure and nothing else:

## The point
One sentence. What a reader learns from this video.

## Key points
Six to twelve bullets. Start every line with "- " and then a timestamp in brackets, like
"- [12:34] The rig holds 90 frames per second.".
State the claim, the number and the name. Keep each bullet under 25 words.

## Facts and numbers
Only the measurements, prices, model names, dates and versions the video states.
Start every line with "- ". Write each line as "value: what it measures", and name the
subject and the unit, like "- 90 fps: the frame rate of the mod in most areas" or
"- Doom 2016: the game the mod converts to virtual reality".
A reader sees these lines alone, so a bare number or a bare name is wrong.
Write "none stated" if the video gives none.

## For me
Two to four bullets. {interest}
Write "nothing relevant" if the video holds nothing for those interests.

## Doubt
One to three bullets. What the video claims without evidence, or where the speaker guesses.
Write "none" if every claim carries evidence.

TRANSCRIPT ({title}, channel {channel}):
{transcript}"""

MERGE = """You have several briefs from consecutive parts of one video. Merge them into one brief.

{style}

Use the same headings: The point, Key points, Facts and numbers, For me, Doubt.
Keep the timestamps. Remove each repeated point. Keep the strongest 12 bullets under Key points.
Under Facts and numbers, keep the "value: what it measures" form, and keep the unit and the subject.

PARTS:
{parts}"""

INTEREST = os.environ.get(
    "YTSUM_INTEREST",
    "The reader works in finance systems and payments, looks for a job, and follows "
    "computer hardware and AI tooling news. Say what this video changes for that reader.")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def metadata(url):
    data = json.loads(run(["yt-dlp", "-J", "--no-warnings", "--skip-download", url]))
    return {"id": data["id"], "title": data.get("title", data["id"]),
            "channel": data.get("uploader", "unknown"),
            "date": data.get("upload_date", ""), "duration": data.get("duration") or 0,
            "url": data.get("webpage_url", url),
            "description": data.get("description") or ""}


def links(description, limit=15):
    """Return [(label, url)] from the video description, in the order they appear."""
    out, seen = [], set()
    for line in description.splitlines():
        for url in re.findall(r"https?://[^\s<>()\[\]\"']+", line):
            url = url.rstrip(".,;:)")
            if url in seen:
                continue
            seen.add(url)
            # The label is the rest of the line. Drop the decoration and the emoji.
            label = re.sub(r"[^\w\s\-()/&.,'+:#]", "", line.replace(url, ""))
            label = re.sub(r"\s+", " ", label).strip()
            label = label.strip("-:·|., ").strip()
            if not label:
                label = re.sub(r"^www\.", "", url.split("/")[2])
            out.append((label, url))
            if len(out) == limit:
                return out
    return out


def captions(url, work):
    """Return the caption file yt-dlp wrote, or None when the video has none."""
    subprocess.run(["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
                    "--sub-langs", "en.*,en", "--convert-subs", "srt", "--no-warnings",
                    "-o", str(work / "cap"), url], capture_output=True, text=True)
    files = sorted(work.glob("cap*.srt"))
    return files[0] if files else None


def whisper(url, work):
    """Fallback for a video with no captions: download the audio and transcribe it."""
    if not shutil.which("whisper-cli"):
        sys.exit("This video has no captions. Install the fallback: brew install whisper-cpp")
    if not Path(WHISPER_MODEL).exists():
        sys.exit(f"No whisper model at {WHISPER_MODEL}. Set YTSUM_WHISPER_MODEL to a ggml .bin file.")
    run(["yt-dlp", "-x", "--audio-format", "m4a", "--no-warnings", "-o", str(work / "a.%(ext)s"), url])
    # whisper.cpp reads 16 kHz mono WAV only. ffmpeg comes with yt-dlp on this machine.
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(work / "a.m4a"),
         "-ar", "16000", "-ac", "1", str(work / "a.wav")])
    run(["whisper-cli", "-m", WHISPER_MODEL, "-f", str(work / "a.wav"),
         "-osrt", "-of", str(work / "a"), "-np"])
    return parse_srt((work / "a.srt").read_text(encoding="utf-8"))


def parse_srt(text):
    """Return [(seconds, line)] from SRT text, with the caption duplicates removed."""
    out, last = [], None
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [x for x in block.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        stamp = re.match(r"(\d\d):(\d\d):(\d\d)[,.](\d+)\s*-->", lines[1] if lines[0].isdigit() else lines[0])
        if not stamp:
            continue
        seconds = int(stamp[1]) * 3600 + int(stamp[2]) * 60 + int(stamp[3])
        # Auto-captions scroll a two-line window: block N holds [A, B] and block
        # N+1 holds [B, C]. Dedupe line by line, or every line arrives twice.
        for line in lines[2:] if lines[0].isdigit() else lines[1:]:
            line = re.sub(r"<[^>]+>", "", line).strip()
            if not line or line == last:
                continue
            fresh = line[len(last):].strip() if last and line.startswith(last) else line
            if fresh:
                out.append((seconds, fresh))
            last = line
    return out


def transcript_text(segments):
    """One text with [mm:ss] every STAMP_EVERY seconds."""
    parts, next_stamp = [], 0
    for seconds, body in segments:
        if seconds >= next_stamp:
            parts.append(f"\n[{seconds // 60:02d}:{seconds % 60:02d}] ")
            next_stamp = seconds + STAMP_EVERY
        parts.append(body + " ")
    return re.sub(r" +", " ", "".join(parts)).strip()


def chunks(text, size=CHUNK_WORDS):
    words = text.split(" ")
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)] or [""]


def ask(prompt, engine, model, ctx=None):
    """One model call. Times it, because a slow call is the failure mode here."""
    start = time.time()
    if engine == "claude":
        reply = run(["claude", "-p", prompt]).strip()
    else:
        # ponytail: no num_ctx unless asked. Passing it forces Ollama to reload the model
        # (11s becomes 42s), and a large value costs minutes. Chunking is cheaper than context.
        options = {"temperature": 0.2, "num_predict": 2000}
        if ctx:
            options["num_ctx"] = ctx
        # think=False matters more than any other setting here. With reasoning on, this model
        # looped to the token cap and returned an EMPTY response after 478s; with it off, the
        # same prompt answers in 15s. num_predict is the second belt.
        body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                           "think": False, "options": options}).encode()
        request = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.load(response)
        reply = (data.get("response") or "").strip()
        if not reply:
            sys.exit(f"The model returned nothing (done_reason={data.get('done_reason')}, "
                     f"{data.get('eval_count')} tokens). Try --model gemma4:latest, or a shorter "
                     f"chunk with YTSUM_CHUNK_WORDS=2000.")
    print(f"  model call: {time.time() - start:.0f}s, {len(reply.split())} words back", file=sys.stderr)
    return reply


def summarise(meta, text, engine, model, ctx=None):
    parts = chunks(text)
    briefs = []
    for i, part in enumerate(parts, 1):
        print(f"  part {i} of {len(parts)}...", file=sys.stderr)
        briefs.append(ask(PROMPT.format(style=STYLE, interest=INTEREST, title=meta["title"],
                                        channel=meta["channel"], transcript=part), engine, model, ctx))
    if len(briefs) == 1:
        return briefs[0]
    print("  merging...", file=sys.stderr)
    joined = "\n\n".join(f"--- PART {i + 1} ---\n{b}" for i, b in enumerate(briefs))
    return ask(MERGE.format(style=STYLE, parts=joined), engine, model, ctx)


def slug(text, limit=60):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:limit]


def write_brief(meta, brief, source, words, engine, model, out_dir=OUT):
    if not brief.strip():
        raise ValueError("refusing to write an empty brief")
    out_dir.mkdir(parents=True, exist_ok=True)
    date = meta["date"] or datetime.date.today().strftime("%Y%m%d")
    path = out_dir / f"{date[:4]}-{date[4:6]}-{date[6:8]}-{slug(meta['channel'], 24)}-{slug(meta['title'])}.md"
    minutes, seconds = divmod(int(meta["duration"]), 60)
    # ponytail: the description holds the mod and product links. No page scrape.
    found = "".join(f"- [{label}]({url})\n" for label, url in links(meta.get("description", "")))
    found = f"\n## Links from the description\n{found}" if found else ""
    path.write_text(
        f"# {meta['title']}\n\n"
        f"{meta['channel']} · {date[:4]}-{date[4:6]}-{date[6:8]} · {minutes}:{seconds:02d} · "
        f"[watch]({meta['url']})\n\n"
        f"Transcript: {source}, {words} words. Brief: {engine} {model}, "
        f"{datetime.date.today().isoformat()}.\n\n---\n\n{brief}\n{found}", encoding="utf-8")
    return path


def one(url, engine, model, ctx=None):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        meta = metadata(url)
        caption_file = captions(url, work)
        if caption_file:
            segments, source = parse_srt(caption_file.read_text(encoding="utf-8", errors="replace")), "captions"
        else:
            segments, source = whisper(url, work), "whisper.cpp"
    text = transcript_text(segments)
    words = len(text.split())
    if words < 30:
        sys.exit(f"Transcript too short to summarise ({words} words): {url}")
    print(f"{meta['title']} — {words} words from {source}, asking {engine}...", file=sys.stderr)
    return write_brief(meta, summarise(meta, text, engine, model, ctx), source, words, engine, model)


def main():
    parser = argparse.ArgumentParser(description="Summarise YouTube videos into Markdown briefs.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--engine", default="ollama", choices=["ollama", "claude"])
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default {MODEL})")
    parser.add_argument("--ctx", type=int, help="force an Ollama context size; costs a model reload, "
                        "and only pays off when it removes several chunks")
    args = parser.parse_args()
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp is not on PATH: brew install yt-dlp")
    for url in args.urls:
        print(one(url, args.engine, args.model, args.ctx))


if __name__ == "__main__":
    main()

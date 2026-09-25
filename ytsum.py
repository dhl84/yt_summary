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

COMMENTS = """You read the comments under a YouTube video. Most say nothing. Find the ones that hold information.

{style}

Keep a comment only when it does one of these:
- it supports a claim in the video with a reason, a measurement or the writer's own result;
- it adds information the video leaves out, such as a newer version, a price, a fault or a step;
- it disputes a claim in the video and says why.

Reject praise, jokes, quotes of the video, greetings, requests, and anything you cannot check.
Reject a claim with no reason behind it. Two comments that say the same thing go in one line.
If the writer says later in the same comment that the problem stopped, say that in the line.
Keep the hardware, the version and the number the writer gives, because the fault can depend on them.

Output this Markdown and nothing else:

## From the comments
Three to eight bullets. Start each line with "- " and then one word in brackets:
"[Supports]", "[Adds]" or "[Disputes]". Then the point in one sentence, then the like count
in brackets, like "- [Adds] Version 1.02 is out already. (31 likes)". A comment that reports a
fault and then says the fault stopped gives one line with both halves, like "- [Adds] The SFS
renderer gave 20 fps on an RTX 4080, and several restarts fixed it. (2 likes)".
Write "- nothing substantive" if no comment holds information.

The video is "{title}". Its main claims:
{claims}

COMMENTS (the like count comes first, and "CREATOR" marks a reply from the channel):
{comments}"""

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


def run(cmd, check=True, **kw):
    # Windows decodes pipes as cp1252 unless told otherwise, and titles hold emoji.
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)
    if check and done.returncode:
        # Say what the tool said. A traceback hides the reason, often an old yt-dlp.
        sys.exit(f"{Path(cmd[0]).name} failed (exit {done.returncode}):\n"
                 f"{done.stderr.strip()[-1500:]}")
    return done.stdout


def metadata(url):
    data = json.loads(run(["yt-dlp", "-J", "--no-warnings", "--skip-download", url]))
    return {"id": data["id"], "title": data.get("title", data["id"]),
            "channel": data.get("uploader", "unknown"),
            "date": data.get("upload_date", ""), "duration": data.get("duration") or 0,
            "url": data.get("webpage_url", url),
            "description": data.get("description") or "",
            "language": data.get("language") or "",
            "subs": list(data.get("subtitles") or {}),
            "auto": list(data.get("automatic_captions") or {})}


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


def caption_tracks(subs, auto, language=""):
    """Return [(kind, code, source)] to try, best first.

    A human English track first, then YouTube's own speech recognition of English
    ("en-orig"), then its English translation, then the speech recognition of the
    video's own language. The model reads any language, but English reads best."""
    tracks = [("subs", c, "captions") for c in subs if c == "en" or c.startswith("en-")]
    tracks += [("auto", c, "YouTube auto-captions") for c in auto if re.fullmatch(r"en(-\w+)?-orig", c)]
    if "en" in auto:
        tracks.append(("auto", "en", "YouTube auto-captions, translated to English"))
    lang = language.split("-")[0]
    others = [c for c in auto if c.endswith("-orig") and not c.startswith("en")]
    others.sort(key=lambda c: c.split("-")[0] != lang)   # the video's own language first
    tracks += [("auto", c, f"YouTube auto-captions ({c})") for c in others]
    return tracks


def captions(url, work, meta):
    """Return (caption file, source) for the best track that downloads, or (None, reason)."""
    tracks = caption_tracks(meta["subs"], meta["auto"], meta["language"])
    if not tracks:
        return None, "YouTube lists no captions for this video, not even auto-captions"
    error = ""
    for kind, code, source in tracks:
        # One track per call. A call for several tracks stops at the first failure, and
        # YouTube often answers the translated "en" track with HTTP 429 while "en-orig" works.
        for attempt in range(2):
            done = subprocess.run(["yt-dlp", "--skip-download",
                                   "--write-subs" if kind == "subs" else "--write-auto-subs",
                                   "--sub-langs", re.escape(code), "--convert-subs", "srt",
                                   "--no-warnings", "-o", str(work / "cap"), url],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")
            files = sorted(work.glob("cap*.srt"))
            if files:
                return files[0], source
            error = (done.stderr.strip().splitlines() or ["yt-dlp wrote no file"])[-1]
            if "429" not in error or attempt:
                break
            print(f"  {code} captions: YouTube says too many requests, retrying in 20s", file=sys.stderr)
            time.sleep(20)
    return None, f"YouTube lists captions, but the download failed ({error})"


def whisper(url, work, reason="This video has no captions"):
    """Fallback for a video with no captions: download the audio and transcribe it."""
    if not shutil.which("whisper-cli"):
        sys.exit(f"{reason}. Install the speech-to-text fallback: brew install whisper-cpp")
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
        # The prompt goes on stdin: Windows caps a command line at 32k characters, and
        # which() finds claude.cmd, which a bare "claude" does not on Windows.
        reply = run([shutil.which("claude") or "claude", "-p"], input=prompt).strip()
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


def comments(url, limit):
    """Return [(likes, text, from_uploader)] for the most liked comments, top first."""
    # ponytail: one yt-dlp call, sorted by top. 60 comments take 4s. No paging, no API key.
    raw = run(["yt-dlp", "-J", "--skip-download", "--write-comments", "--no-warnings",
               "--extractor-args", f"youtube:comment_sort=top;max_comments={limit},all,{limit},0",
               url], check=False)
    if not raw:
        return []
    found = (json.loads(raw).get("comments") or [])
    out = []
    for c in found:
        text = re.sub(r"\s+", " ", (c.get("text") or "")).strip()
        if len(text) < 15:
            continue
        out.append((c.get("like_count") or 0, text[:400], bool(c.get("author_is_uploader"))))
    out.sort(key=lambda x: -x[0])
    return out


def comment_brief(meta, found, brief, engine, model, ctx=None):
    """Ask the model which comments hold information. Returns a Markdown section or ""."""
    if not found:
        return ""
    lines = "\n".join(f"{likes} likes{' CREATOR' if mine else ''}: {text}"
                       for likes, text, mine in found)
    claims = "\n".join(l for l in brief.splitlines() if l.startswith("- ["))[:2000]
    print(f"  {len(found)} comments, asking {engine}...", file=sys.stderr)
    return ask(COMMENTS.format(style=STYLE, title=meta["title"], claims=claims,
                               comments=lines), engine, model, ctx)


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


def write_brief(meta, brief, source, words, engine, model, out_dir=OUT, extra=""):
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
        f"{datetime.date.today().isoformat()}.\n\n---\n\n{brief}\n{extra}{found}", encoding="utf-8")
    return path


def one(url, engine, model, ctx=None, comment_limit=60):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        meta = metadata(url)
        caption_file, source = captions(url, work, meta)
        if caption_file:
            segments = parse_srt(caption_file.read_text(encoding="utf-8", errors="replace"))
        else:
            print(f"  {source}. Trying whisper.cpp.", file=sys.stderr)
            segments, source = whisper(url, work, source), "whisper.cpp"
    text = transcript_text(segments)
    words = len(text.split())
    if words < 30:
        sys.exit(f"Transcript too short to summarise ({words} words): {url}")
    print(f"{meta['title']} — {words} words from {source}, asking {engine}...", file=sys.stderr)
    brief = summarise(meta, text, engine, model, ctx)
    extra = ""
    if comment_limit:
        found = comments(url, comment_limit)
        section = comment_brief(meta, found, brief, engine, model, ctx)
        extra = f"\n{section}\n" if section else ""
    return write_brief(meta, brief, source, words, engine, model, extra=extra)


def main():
    parser = argparse.ArgumentParser(description="Summarise YouTube videos into Markdown briefs.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--engine", default="ollama", choices=["ollama", "claude"])
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default {MODEL})")
    parser.add_argument("--ctx", type=int, help="force an Ollama context size; costs a model reload, "
                        "and only pays off when it removes several chunks")
    parser.add_argument("--comments", type=int, default=60, metavar="N",
                        help="read the N most liked comments and summarise the ones with "
                             "information (default 60, 0 to skip)")
    args = parser.parse_args()
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp is not on PATH: brew install yt-dlp")
    # yt-dlp needs ffmpeg to turn the captions into SRT. Without it every video looks caption-less.
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is not on PATH: brew install ffmpeg")
    for url in args.urls:
        print(one(url, args.engine, args.model, args.ctx, args.comments))


if __name__ == "__main__":
    main()

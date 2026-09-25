#!/usr/bin/env python3
"""Offline checks. No network, no model. Run: python3 test_ytsum.py"""
import json
import tempfile
from pathlib import Path
import ytsum

# Auto-captions scroll: each block repeats the line before it. The parser must not repeat text.
SRT = """1
00:00:01,000 --> 00:00:03,000
the fan curve is flat

2
00:00:03,000 --> 00:00:05,000
the fan curve is flat until 60 degrees

3
00:00:40,000 --> 00:00:42,000
<i>it costs 349 dollars</i>

4
00:00:42,000 --> 00:00:44,000
it costs 349 dollars
"""

# The real scroll from YouTube is a two-line window: block N holds [A, B] and
# block N+1 holds [B, C]. This fixture came from a live caption file.
WINDOW = """1
00:00:00,560 --> 00:00:03,750
Yo, hello folks. Welcome back to the

2
00:00:03,760 --> 00:00:08,230
Yo, hello folks. Welcome back to the
channel. I am the happiest

3
00:00:08,240 --> 00:00:11,910
channel. I am the happiest
man in the world and nobody
"""


DESCRIPTION = """Big changes in the mod.

👉 Mod can be found here: https://github.com/CactusVRStudios/KHARVOX/releases
And here: https://mrsurvivor-installers.com/ (after launch)
https://store.steampowered.com/app/2554800/Cactus/
Same link again: https://mrsurvivor-installers.com/
See the docs at https://example.com/a.
"""


def main():
    segments = ytsum.parse_srt(SRT)
    text = ytsum.transcript_text(segments)
    assert "the fan curve is flat until 60 degrees" in text, text
    assert text.count("the fan curve is flat") == 1, f"duplicate caption text: {text}"
    assert text.count("349 dollars") == 1, f"duplicate caption text: {text}"
    assert "<i>" not in text, "markup survived"
    assert text.startswith("[00:01]"), text
    assert "[00:40]" in text, f"missing the half-minute stamp: {text}"

    window = ytsum.transcript_text(ytsum.parse_srt(WINDOW))
    assert window.count("Welcome back to the") == 1, f"two-line window doubled: {window}"
    assert window.count("I am the happiest") == 1, f"two-line window doubled: {window}"
    assert "man in the world and nobody" in window, window

    # comments(): the shaping, with yt-dlp replaced. Short comments go, top likes first.
    payload = {"comments": [
        {"text": "nice", "like_count": 900},
        {"text": "Version 1.02 is out already", "like_count": 31},
        {"text": "The  renderer\ngives 20 fps on\nan RTX 4080", "like_count": 2},
        {"text": "Thanks for testing this, it answers my question", "like_count": 5,
         "author_is_uploader": True},
    ]}
    real_run, ytsum.run = ytsum.run, lambda cmd, **kw: json.dumps(payload)
    try:
        found = ytsum.comments("https://youtu.be/x", 60)
        assert [c[0] for c in found] == [31, 5, 2], found
        assert found[0][1] == "Version 1.02 is out already", found[0]
        assert found[2][1] == "The renderer gives 20 fps on an RTX 4080", found[2]
        assert found[1][2] is True, "the uploader flag is lost"
        ytsum.run = lambda cmd, **kw: ""
        assert ytsum.comments("https://youtu.be/x", 60) == []
    finally:
        ytsum.run = real_run
    assert ytsum.comment_brief({"title": "x"}, [], "", "ollama", "m") == ""

    found = ytsum.links(DESCRIPTION)
    assert found[0] == ("Mod can be found here", "https://github.com/CactusVRStudios/KHARVOX/releases"), found[0]
    assert found[1] == ("And here: (after launch)", "https://mrsurvivor-installers.com/"), found[1]
    assert found[2][0] == "store.steampowered.com", found[2]
    assert [u for _, u in found].count("https://mrsurvivor-installers.com/") == 1, "duplicate link"
    assert ("See the docs at", "https://example.com/a") in found, found
    assert ytsum.links("") == []
    assert len(ytsum.links(DESCRIPTION, limit=2)) == 2

    # caption_tracks(): a human track first, then YouTube's English speech recognition,
    # then the translation, then the video's own language before the other dubs.
    auto = ["ar-orig", "en", "de-DE-orig", "en-orig", "fr-FR-orig", "fr"]
    order = [code for _, code, _ in ytsum.caption_tracks(["en-GB", "live_chat"], auto, "fr-FR")]
    assert order == ["en-GB", "en-orig", "en", "fr-FR-orig", "ar-orig", "de-DE-orig"], order
    assert ytsum.caption_tracks([], ["en-orig"])[0] == ("auto", "en-orig", "YouTube auto-captions")
    assert ytsum.caption_tracks([], []) == []

    assert ytsum.parse_srt("") == []
    assert ytsum.chunks("") == [""]
    assert [len(c.split()) for c in ytsum.chunks(" ".join("w" * 5 for _ in range(10)), size=4)] == [4, 4, 2]
    assert ytsum.slug("Gamers Nexus: RTX 5090 (review!)") == "gamers-nexus-rtx-5090-review"

    meta = {"title": "RTX 5090 review", "channel": "Gamers Nexus", "date": "20260919",
            "duration": 3725, "url": "https://youtu.be/x"}
    with tempfile.TemporaryDirectory() as tmp:
        path = ytsum.write_brief(meta, "## The point\nIt is fast.", "captions", 9000,
                                 "ollama", "gemma4", out_dir=Path(tmp))
        body = path.read_text()
    assert path.name == "2026-09-19-gamers-nexus-rtx-5090-review.md", path.name
    assert "62:05" in body, "duration wrong"
    assert "[watch](https://youtu.be/x)" in body and "captions, 9000 words" in body
    # An empty model reply must never reach the disk as a brief.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ytsum.write_brief(meta, "   \n", "captions", 10, "ollama", "gemma4", out_dir=Path(tmp))
        raise AssertionError("write_brief accepted an empty brief")
    except ValueError:
        pass

    print("ok")


if __name__ == "__main__":
    main()

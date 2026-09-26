import base64
import json
import shutil
import subprocess
import threading

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4

import app


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def require_media_tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe not available")
    return ffmpeg, ffprobe


def make_source_flac(path):
    ffmpeg, _ = require_media_tools()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-ac",
            "2",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )

    audio = FLAC(path)
    # Deliberately omit TITLE so output must use the filename-inferred title.
    audio["artist"] = ["Artist A", "Artist B"]
    audio["album"] = ["Test Album"]
    audio["albumartist"] = ["Album Artist A", "Album Artist B"]
    audio["tracknumber"] = ["2/10"]
    audio["discnumber"] = ["1/2"]
    audio["genre"] = ["Test Genre"]
    audio["date"] = ["2026"]
    audio["bpm"] = ["123"]
    audio["compilation"] = ["1"]
    audio["customtag"] = ["custom value"]

    picture = Picture()
    picture.type = 3
    picture.mime = "image/png"
    picture.desc = "Cover"
    picture.data = PNG_1X1
    audio.add_picture(picture)
    audio.save()


def test_end_to_end_flac_to_aac_with_metadata_artwork_and_lyrics(tmp_path):
    ffmpeg, ffprobe = require_media_tools()

    source = tmp_path / "Filename Artist - Fallback Title.flac"
    make_source_flac(source)

    lrc = tmp_path / "Filename Artist - Fallback Title.lrc"
    lrc.write_text(
        "[ar:Artist A]\n"
        "[ti:Fallback Title]\n"
        "[al:Test Album]\n"
        "[00:01.00]First line\n"
        "<00:02.00>Second line\n",
        encoding="utf-8",
    )

    rows, _, duplicate_count = app.scan_library(tmp_path)
    assert duplicate_count == 0
    assert len(rows) == 1

    row = rows[0]
    assert row.flac.title == "Fallback Title"
    assert row.flac.artist == "Artist A, Artist B"
    assert row.status == "가사 있음"
    assert row.lrc is not None

    out_dir = tmp_path / "output"
    state, message, out = app.convert_one(
        row,
        out_dir,
        ffmpeg,
        overwrite=True,
        cancel_event=threading.Event(),
    )

    assert state == "converted", message
    assert out.exists()
    assert out.parent == out_dir
    assert len(out.relative_to(out_dir).parts) == 1
    assert out.name == "Artist A, Artist B - Fallback Title.m4a"

    mp4 = MP4(out)
    assert mp4.tags["\xa9nam"] == ["Fallback Title"]
    assert mp4.tags["\xa9ART"] == ["Artist A, Artist B"]
    assert mp4.tags["\xa9alb"] == ["Test Album"]
    assert mp4.tags["aART"] == ["Album Artist A, Album Artist B"]
    assert mp4.tags["trkn"] == [(2, 10)]
    assert mp4.tags["disk"] == [(1, 2)]
    assert mp4.tags["\xa9gen"] == ["Test Genre"]
    assert mp4.tags["\xa9day"] == ["2026"]
    assert mp4.tags["tmpo"] == [123]
    assert mp4.tags["cpil"] == [True]
    assert mp4.tags["\xa9lyr"] == ["First line\nSecond line"]
    assert mp4.tags["covr"]
    assert "----:com.apple.iTunes:FLAC_CUSTOMTAG" in mp4.tags

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,profile",
            "-of",
            "json",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "aac"
    assert "LC" in stream.get("profile", "")

    # Existing output is reported separately instead of being counted as success.
    state, _, same_out = app.convert_one(
        row,
        out_dir,
        ffmpeg,
        overwrite=False,
        cancel_event=threading.Event(),
    )
    assert state == "skipped"
    assert same_out == out

    # Cancellation is recognized before work starts.
    cancelled = threading.Event()
    cancelled.set()
    state, _, _ = app.convert_one(
        row,
        out_dir,
        ffmpeg,
        overwrite=True,
        cancel_event=cancelled,
    )
    assert state == "cancelled"

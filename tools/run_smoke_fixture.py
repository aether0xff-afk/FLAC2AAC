from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def media_tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg / ffprobe가 PATH에 필요합니다.")
    return ffmpeg, ffprobe


def make_base_flac(path: Path, ffmpeg: str):
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
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "0.08",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def recreate_library(fixture: Path, work_root: Path, ffmpeg: str) -> dict:
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))

    base = work_root / "__base.flac"
    make_base_flac(base, ffmpeg)

    for i, record in enumerate(manifest["flacs"], 1):
        destination = work_root / Path(record["relative_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base, destination)

        audio = FLAC(destination)
        audio.clear()

        for tag, values in record.get("tags", {}).items():
            try:
                audio[tag] = [str(v) for v in values]
            except Exception:
                # Keep smoke generation going even if a very unusual tag key
                # is rejected by the current Mutagen version.
                pass

        if record.get("pictures"):
            picture = Picture()
            picture.type = 3
            picture.mime = "image/png"
            picture.desc = "Smoke fixture cover"
            picture.data = PNG_1X1
            audio.add_picture(picture)

        audio.save()

        if i % 250 == 0 or i == len(manifest["flacs"]):
            print(f"synthetic FLAC {i}/{len(manifest['flacs'])}")

    base.unlink(missing_ok=True)

    copied = 0
    for record in manifest["lrcs"]:
        relative = Path(record["relative_path"])
        source = fixture / "lrc" / relative
        if not source.exists():
            raise FileNotFoundError(f"fixture LRC missing: {source}")
        destination = work_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1

    return manifest


def diversity_score(row: app.Row) -> tuple:
    tags = row.flac.tags
    text = f"{row.flac.artist} {row.flac.title} {row.flac.path.name}"
    non_ascii = any(ord(ch) > 127 for ch in text)
    multiple_artist = len(row.flac.artists) > 1
    no_title_tag = not tags.get("title")
    no_artist_tag = not tags.get("artist")
    rich = sum(
        bool(tags.get(k))
        for k in (
            "album",
            "albumartist",
            "tracknumber",
            "discnumber",
            "genre",
            "date",
            "bpm",
            "compilation",
        )
    )
    return (
        int(row.status == "가사 있음"),
        int(multiple_artist),
        int(no_title_tag),
        int(no_artist_tag),
        int(non_ascii),
        rich,
        len(tags),
    )


def choose_rows(rows: list[app.Row], limit: int) -> list[app.Row]:
    ranked = sorted(
        rows,
        key=lambda r: (diversity_score(r), str(r.flac.path).casefold()),
        reverse=True,
    )

    chosen = []
    seen = set()
    for row in ranked:
        k = row.flac.path
        if k in seen:
            continue
        chosen.append(row)
        seen.add(k)
        if len(chosen) >= limit:
            break
    return chosen


def validate_output(row: app.Row, path: Path, ffprobe: str):
    mp4 = MP4(path)
    tags = mp4.tags or {}

    if row.flac.title:
        assert tags.get("\xa9nam") == [row.flac.title], (row.flac.path, tags.get("\xa9nam"))
    if row.flac.artist:
        assert tags.get("\xa9ART") == [row.flac.artist], (row.flac.path, tags.get("\xa9ART"))
        assert ";" not in tags["\xa9ART"][0]

    if row.status == "가사 있음":
        assert tags.get("\xa9lyr"), f"lyrics missing: {row.flac.path}"

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
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "aac"
    assert "LC" in stream.get("profile", "")


def run(fixture: Path, convert_limit: int, workers: int) -> dict:
    ffmpeg, ffprobe = media_tools()
    fixture = fixture.resolve()

    if not (fixture / "manifest.json").is_file():
        raise FileNotFoundError(f"manifest.json 없음: {fixture}")

    with tempfile.TemporaryDirectory(prefix="flac2aac_real_smoke_") as temp_dir:
        root = Path(temp_dir) / "library"
        root.mkdir(parents=True)
        manifest = recreate_library(fixture, root, ffmpeg)

        rows, lrcs, duplicate_count = app.scan_library(root)
        selected = choose_rows(rows, min(convert_limit, len(rows)))

        out = Path(temp_dir) / "output"
        results = []

        def one(row: app.Row):
            state, message, output = app.convert_one(
                row,
                out,
                ffmpeg,
                overwrite=True,
                cancel_event=threading.Event(),
            )
            if state != "converted":
                raise RuntimeError(f"{row.flac.label}: {state}: {message}")
            validate_output(row, output, ffprobe)
            return row

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
            futures = [executor.submit(one, row) for row in selected]
            for i, future in enumerate(as_completed(futures), 1):
                row = future.result()
                results.append(row.flac.label)
                print(f"convert+validate {i}/{len(futures)}: {row.flac.label}")

        summary = {
            "fixture_flacs": manifest["flac_count"],
            "fixture_lrcs": manifest["lrc_count"],
            "scan_rows_after_dedup": len(rows),
            "duplicate_count": duplicate_count,
            "lyrics_matched": sum(r.status == "가사 있음" for r in rows),
            "lyrics_missing": sum(r.status == "가사 없음" for r in rows),
            "converted_and_validated": len(results),
            "selected_examples": results,
        }
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild a synthetic library from private metadata/LRC fixture and smoke-test FLAC2AAC."
    )
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=Path(".smoke-data"),
    )
    parser.add_argument("--convert-limit", type=int, default=30)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, max(2, (os.cpu_count() or 4) // 2)),
    )
    args = parser.parse_args()

    try:
        summary = run(args.fixture, args.convert_limit, args.workers)
    except Exception as exc:
        print(f"SMOKE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = args.fixture / "last-smoke-report.json"
    report.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\nSMOKE PASS")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from mutagen.flac import FLAC


SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_text_encoding(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"

    if raw and raw.count(b"\x00") / len(raw) > 0.10:
        encodings = ("utf-16-le", "utf-16-be", "utf-8", "cp949", "euc-kr")
    else:
        encodings = ("utf-8", "cp949", "euc-kr", "utf-16-le", "utf-16-be")

    for encoding in encodings:
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            pass
    return "unknown"


def flac_record(root: Path, path: Path) -> dict:
    relative = path.relative_to(root).as_posix()
    record = {
        "relative_path": relative,
        "file_size": path.stat().st_size,
        "tags": {},
        "audio": {},
        "pictures": [],
        "parse_error": None,
    }

    try:
        audio = FLAC(path)
        record["tags"] = {
            str(k): [str(v) for v in values]
            for k, values in (audio.tags.items() if audio.tags else [])
        }
        record["audio"] = {
            "sample_rate": int(getattr(audio.info, "sample_rate", 0) or 0),
            "bits_per_sample": int(getattr(audio.info, "bits_per_sample", 0) or 0),
            "channels": int(getattr(audio.info, "channels", 0) or 0),
            "length": round(float(getattr(audio.info, "length", 0.0) or 0.0), 3),
        }
        record["pictures"] = [
            {
                "type": int(getattr(pic, "type", 0) or 0),
                "mime": str(getattr(pic, "mime", "") or ""),
                "description": str(getattr(pic, "desc", "") or ""),
                "width": int(getattr(pic, "width", 0) or 0),
                "height": int(getattr(pic, "height", 0) or 0),
                "depth": int(getattr(pic, "depth", 0) or 0),
                "bytes": len(pic.data),
                "sha256": hashlib.sha256(pic.data).hexdigest(),
            }
            for pic in audio.pictures
        ]
    except Exception as exc:
        record["parse_error"] = f"{type(exc).__name__}: {exc}"

    return record


def collect(root: Path, output: Path, workers: int) -> dict:
    root = root.resolve()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lrc_output = output / "lrc"
    lrc_output.mkdir(parents=True, exist_ok=True)

    flac_paths = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".flac"),
        key=lambda p: str(p).casefold(),
    )
    lrc_paths = sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".lrc"),
        key=lambda p: str(p).casefold(),
    )

    print(f"FLAC: {len(flac_paths)}")
    print(f"LRC : {len(lrc_paths)}")
    print(f"Output: {output}")

    records: list[dict] = []
    max_workers = max(1, min(workers, 32))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(flac_record, root, p): p for p in flac_paths}
        done = 0
        for future in as_completed(futures):
            records.append(future.result())
            done += 1
            if done % 100 == 0 or done == len(futures):
                print(f"metadata {done}/{len(futures)}")

    records.sort(key=lambda r: r["relative_path"].casefold())

    lrc_records = []
    for index, source in enumerate(lrc_paths, 1):
        relative = source.relative_to(root)
        destination = lrc_output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        lrc_records.append(
            {
                "relative_path": relative.as_posix(),
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
                "encoding": detect_text_encoding(source),
            }
        )
        if index % 200 == 0 or index == len(lrc_paths):
            print(f"lrc copy {index}/{len(lrc_paths)}")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_root_name": root.name,
        "source_root_absolute_path_stored": False,
        "flac_count": len(records),
        "lrc_count": len(lrc_records),
        "flacs": records,
        "lrcs": lrc_records,
    }

    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = {
        "manifest": str(manifest_path),
        "flacs": len(records),
        "lrcs": len(lrc_records),
        "flac_parse_errors": sum(bool(r["parse_error"]) for r in records),
        "multiple_artist_tags": sum(
            len(r["tags"].get("artist", [])) > 1
            or any(";" in v for v in r["tags"].get("artist", []))
            for r in records
        ),
        "missing_title_tags": sum(not r["tags"].get("title") for r in records),
        "missing_artist_tags": sum(not r["tags"].get("artist") for r in records),
        "with_artwork": sum(bool(r["pictures"]) for r in records),
    }
    (output / "collection-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect FLAC metadata and raw LRC files for private smoke testing."
    )
    parser.add_argument("music_root", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(".smoke-data"),
        help="Private fixture directory (default: .smoke-data)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(16, max(4, os.cpu_count() or 4)),
    )
    args = parser.parse_args()

    if not args.music_root.is_dir():
        print(f"Not a directory: {args.music_root}", file=sys.stderr)
        return 2

    summary = collect(args.music_root, args.output, args.workers)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

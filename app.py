from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.mp4 import AtomDataType, MP4, MP4Cover, MP4FreeForm
from PyQt6.QtCore import QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP = "FLAC → Apple Music AAC"

META_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_-]*):(.*?)\]\s*$")
TS_RE = re.compile(
    r"\[(?:\d{1,3}:)?\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\]"
    r"|\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]"
)
WORD_TS_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
WINDOWS_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def display_text(value: str) -> str:
    """Normalize Unicode/whitespace without changing user-visible case."""
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", value).strip()


def norm(value: str) -> str:
    return display_text(value).casefold()


def key(value: str) -> str:
    """Loose matching key: Unicode letters/numbers, case/spacing/diacritics ignored."""
    folded = unicodedata.normalize("NFKD", norm(value))
    return "".join(
        ch for ch in folded
        if ch.isalnum() and not unicodedata.combining(ch)
    )


def split_artist_values(values) -> tuple[str, ...]:
    """Flatten repeated ARTIST values and semicolon-separated values."""
    parts: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for part in re.split(r"\s*;\s*", display_text(str(value))):
            part = part.strip()
            k = norm(part)
            if part and k not in seen:
                seen.add(k)
                parts.append(part)
    return tuple(parts)


def join_artist_values(values) -> str:
    """Display multiple artists with a comma, never a semicolon."""
    return ", ".join(split_artist_values(values))


def infer_filename_fields(path: Path) -> tuple[str, str]:
    """
    Return (artist, title) inferred from the filename while preserving case.
    'Artist - Title.flac' -> ('Artist', 'Title')
    """
    stem = display_text(path.stem)
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", stem


def inferred_title(path: Path) -> str:
    return infer_filename_fields(path)[1]


def inferred_artist(path: Path) -> str:
    return infer_filename_fields(path)[0]


def read_text(path: Path) -> str:
    raw = path.read_bytes()

    # BOM-aware decoding first.
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")

    # UTF-16 files without a BOM often contain many NUL bytes. Try the likely
    # byte order before legacy Korean encodings so they are not mis-decoded.
    if raw and raw.count(b"\x00") / len(raw) > 0.10:
        encodings = ("utf-16-le", "utf-16-be", "utf-8", "cp949", "euc-kr")
    else:
        encodings = ("utf-8", "cp949", "euc-kr", "utf-16-le", "utf-16-be")

    for enc in encodings:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


@dataclass
class FlacItem:
    path: Path
    title: str = ""
    artist: str = ""
    artists: tuple[str, ...] = field(default_factory=tuple)
    tags: dict[str, list[str]] = field(default_factory=dict)
    quality_score: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.path.stem


@dataclass
class LrcItem:
    path: Path
    title: str
    artist: str
    text: str


@dataclass
class Row:
    flac: FlacItem
    lrc: LrcItem | None
    status: str
    output_name: str = ""


def parse_lrc(path: Path) -> LrcItem:
    text = read_text(path)
    tags: dict[str, str] = {}
    for line in text.splitlines():
        match = META_RE.match(line)
        if match and match.group(1).lower() not in tags:
            tags[match.group(1).lower()] = display_text(match.group(2))
    title = tags.get("ti", "") or inferred_title(path)
    return LrcItem(path, title, tags.get("ar", ""), text)


def clean_lrc(text: str) -> str:
    out: list[str] = []
    blank = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.lstrip("\ufeff")
        if META_RE.match(line):
            continue
        line = WORD_TS_RE.sub("", TS_RE.sub("", line)).strip()
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def first(tags: dict[str, list[str]], *names: str) -> str:
    for name in names:
        vals = tags.get(name.lower())
        if vals:
            return display_text(str(vals[0]))
    return ""


def load_flac(path: Path) -> FlacItem:
    inferred_a, inferred_t = infer_filename_fields(path)
    try:
        audio = FLAC(path)
        tags = {
            k.lower(): [str(v) for v in vals]
            for k, vals in (audio.tags.items() if audio.tags else [])
        }

        title = first(tags, "title") or inferred_t
        artists = split_artist_values(tags.get("artist", []))
        if not artists and inferred_a:
            artists = (inferred_a,)
        artist = ", ".join(artists)

        has_art = 1 if getattr(audio, "pictures", None) else 0
        tag_count = sum(
            1 for vals in tags.values()
            if any(str(v).strip() for v in vals)
        )
        bits = int(getattr(audio.info, "bits_per_sample", 0) or 0)
        sample_rate = int(getattr(audio.info, "sample_rate", 0) or 0)
        size = path.stat().st_size if path.exists() else 0
        quality = (has_art, tag_count, bits, sample_rate, size)
        return FlacItem(path, title, artist, artists, tags, quality)
    except Exception:
        size = path.stat().st_size if path.exists() else 0
        artists = (inferred_a,) if inferred_a else ()
        return FlacItem(
            path,
            inferred_t,
            inferred_a,
            artists,
            {},
            (0, 0, 0, 0, size),
        )


def canonical_artist_key(item: FlacItem) -> str:
    parts = item.artists or split_artist_values([item.artist])
    keys = sorted({key(part) for part in parts if key(part)})
    return "&".join(keys)


def dedup_key(item: FlacItem) -> str:
    title_key = key(item.title or inferred_title(item.path))
    if not title_key:
        return ""
    artist_key = canonical_artist_key(item)
    return f"{artist_key}::{title_key}" if artist_key else f"::{title_key}"


def deduplicate_flacs(flacs: list[FlacItem]) -> tuple[list[FlacItem], int]:
    groups: dict[str, list[FlacItem]] = {}
    unique: list[FlacItem] = []

    for item in flacs:
        duplicate_key = dedup_key(item)
        if not duplicate_key:
            unique.append(item)
        else:
            groups.setdefault(duplicate_key, []).append(item)

    kept = list(unique)
    removed = 0
    for group in groups.values():
        # Better metadata/source quality wins. Ties are deterministic.
        best = sorted(
            group,
            key=lambda item: (
                tuple(-v for v in item.quality_score),
                len(str(item.path)),
                str(item.path).casefold(),
            ),
        )[0]
        kept.append(best)
        removed += len(group) - 1

    kept.sort(key=lambda item: str(item.path).casefold())
    return kept, removed


def lrc_artist_score(flac: FlacItem, lrc: LrcItem) -> int:
    if not flac.artist or not lrc.artist:
        return 0

    lrc_key = key(lrc.artist)
    if not lrc_key:
        return 0
    if lrc_key == key(flac.artist):
        return 2

    # Partial credit only for exact artist components, never substring matches
    # such as "A" accidentally matching "Adele".
    lrc_parts = {
        key(part)
        for part in re.split(r"\s*[;,]\s*", lrc.artist)
        if key(part)
    }
    flac_parts = {
        key(part)
        for part in (flac.artists or split_artist_values([flac.artist]))
        if key(part)
    }
    return 1 if flac_parts & lrc_parts else 0


def same_parent(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a.parent)) == os.path.normcase(
        os.path.abspath(b.parent)
    )


def assign_lrcs_one_to_one(
    flacs: list[FlacItem], lrcs: list[LrcItem]
) -> dict[Path, LrcItem]:
    """
    Globally allocate each LRC at most once.

    Matching remains title-first and permissive. When several tracks/candidates
    share a title, the strongest deterministic pairs are reserved first.
    """
    lrc_by_title: dict[str, list[LrcItem]] = {}
    for lrc in lrcs:
        title_key = key(lrc.title)
        if title_key:
            lrc_by_title.setdefault(title_key, []).append(lrc)

    flac_by_title: dict[str, list[FlacItem]] = {}
    for flac in flacs:
        title_key = key(flac.title)
        if title_key:
            flac_by_title.setdefault(title_key, []).append(flac)

    assigned: dict[Path, LrcItem] = {}

    for title_key, title_flacs in flac_by_title.items():
        candidates = lrc_by_title.get(title_key, [])
        if not candidates:
            continue

        edges = []
        for flac in title_flacs:
            for lrc in candidates:
                exact_stem = int(key(flac.path.stem) == key(lrc.path.stem))
                in_same_dir = int(same_parent(flac.path, lrc.path))
                artist_score = lrc_artist_score(flac, lrc)
                edges.append(
                    (
                        exact_stem,
                        in_same_dir,
                        artist_score,
                        str(flac.path).casefold(),
                        str(lrc.path).casefold(),
                        flac,
                        lrc,
                    )
                )

        edges.sort(key=lambda e: (-e[0], -e[1], -e[2], e[3], e[4]))
        used_flacs: set[Path] = set()
        used_lrcs: set[Path] = set()

        for _, _, _, _, _, flac, lrc in edges:
            if flac.path in used_flacs or lrc.path in used_lrcs:
                continue
            assigned[flac.path] = lrc
            used_flacs.add(flac.path)
            used_lrcs.add(lrc.path)

    return assigned


def safe_output_stem(text: str) -> str:
    stem = WINDOWS_BAD_NAME_RE.sub("_", display_text(text)).strip().rstrip(". ")
    if not stem:
        stem = "Untitled"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = "_" + stem
    # Keep margin for extension and full-path overhead on Windows.
    return stem[:180].rstrip(". ") or "Untitled"


def assign_flat_output_names(rows: list[Row]) -> None:
    used: set[str] = set()
    for row in rows:
        if row.flac.artist and row.flac.title:
            base = f"{row.flac.artist} - {row.flac.title}"
        elif row.flac.title:
            base = row.flac.title
        else:
            base = row.flac.path.stem

        base = safe_output_stem(base)
        name = f"{base}.m4a"
        number = 2
        while name.casefold() in used:
            name = f"{base} ({number}).m4a"
            number += 1
        used.add(name.casefold())
        row.output_name = name


def _safe_lrc(path: Path) -> LrcItem:
    try:
        return parse_lrc(path)
    except Exception:
        return LrcItem(path, inferred_title(path), "", "")


def scan_library(
    root: Path, progress=None
) -> tuple[list[Row], list[LrcItem], int]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".flac", ".lrc"}
    ]
    flac_paths = sorted(
        (p for p in files if p.suffix.casefold() == ".flac"),
        key=lambda p: str(p).casefold(),
    )
    lrc_paths = sorted(
        (p for p in files if p.suffix.casefold() == ".lrc"),
        key=lambda p: str(p).casefold(),
    )

    total = len(flac_paths) + len(lrc_paths)
    done = 0
    flacs: list[FlacItem] = []
    lrcs: list[LrcItem] = []

    # Metadata parsing is mostly file I/O. Parallelizing it substantially speeds
    # up large libraries without increasing FFmpeg load.
    scan_workers = min(16, max(4, os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=scan_workers) as executor:
        future_map = {
            executor.submit(load_flac, path): ("flac", path)
            for path in flac_paths
        }
        future_map.update(
            {
                executor.submit(_safe_lrc, path): ("lrc", path)
                for path in lrc_paths
            }
        )

        for future in as_completed(future_map):
            kind, path = future_map[future]
            result = future.result()
            if kind == "flac":
                flacs.append(result)
            else:
                lrcs.append(result)
            done += 1
            if progress:
                progress(done, total, path.name)

    flacs.sort(key=lambda item: str(item.path).casefold())
    lrcs.sort(key=lambda item: str(item.path).casefold())

    flacs, duplicate_count = deduplicate_flacs(flacs)
    allocations = assign_lrcs_one_to_one(flacs, lrcs)

    rows = []
    for flac in flacs:
        lrc = allocations.get(flac.path)
        rows.append(Row(flac, lrc, "가사 있음" if lrc else "가사 없음"))

    assign_flat_output_names(rows)
    return rows, lrcs, duplicate_count


STD_OTHER = {
    "album": "\xa9alb",
    "composer": "\xa9wrt",
    "date": "\xa9day",
    "year": "\xa9day",
    "comment": "\xa9cmt",
    "description": "desc",
    "grouping": "\xa9grp",
    "genre": "\xa9gen",
    "copyright": "cprt",
    "titlesort": "sonm",
    "title sort": "sonm",
    "albumsort": "soal",
    "album sort": "soal",
    "artistsort": "soar",
    "artist sort": "soar",
    "albumartistsort": "soaa",
    "album artist sort": "soaa",
    "composersort": "soco",
    "composer sort": "soco",
}
KNOWN = set(STD_OTHER) | {
    "title",
    "artist",
    "albumartist",
    "album artist",
    "tracknumber",
    "tracktotal",
    "totaltracks",
    "discnumber",
    "disctotal",
    "totaldiscs",
    "bpm",
    "tempo",
    "compilation",
    "lyrics",
    "unsyncedlyrics",
}


def num_total(value: str, total: str = "") -> tuple[int, int]:
    match = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", value or "")
    if not match:
        return 0, int(total) if str(total).isdigit() else 0
    number = int(match.group(1))
    parsed_total = (
        int(match.group(2))
        if match.group(2)
        else (int(total) if str(total).isdigit() else 0)
    )
    return number, parsed_total


def copy_meta(item: FlacItem, dst_path: Path, lrc: LrcItem | None) -> None:
    src = FLAC(item.path)
    dst = MP4(dst_path)
    if dst.tags is None:
        dst.add_tags()
    dst.tags.clear()
    tags = dst.tags

    source_tags = {
        k.lower(): [str(v) for v in vals]
        for k, vals in (src.tags.items() if src.tags else [])
    }

    # Always fill title/artist from the resolved library item when source tags
    # are missing. This prevents inferred display data from being lost.
    title = first(source_tags, "title") or item.title
    if title:
        tags["\xa9nam"] = [title]

    artist = join_artist_values(source_tags.get("artist", [])) or item.artist
    if artist:
        tags["\xa9ART"] = [artist]

    album_artist_values = (
        source_tags.get("albumartist")
        or source_tags.get("album artist")
        or []
    )
    album_artist = join_artist_values(album_artist_values)
    if album_artist:
        tags["aART"] = [album_artist]

    seen_atoms: set[str] = set()
    for source_key, atom in STD_OTHER.items():
        values = source_tags.get(source_key)
        if values and atom not in seen_atoms:
            tags[atom] = values
            seen_atoms.add(atom)

    track = num_total(
        first(source_tags, "tracknumber"),
        first(source_tags, "tracktotal", "totaltracks"),
    )
    disc = num_total(
        first(source_tags, "discnumber"),
        first(source_tags, "disctotal", "totaldiscs"),
    )
    if track != (0, 0):
        tags["trkn"] = [track]
    if disc != (0, 0):
        tags["disk"] = [disc]

    bpm = first(source_tags, "bpm", "tempo")
    if bpm:
        try:
            tags["tmpo"] = [max(0, min(65535, int(round(float(bpm)))))]
        except ValueError:
            pass

    compilation = first(source_tags, "compilation")
    if compilation:
        tags["cpil"] = [
            compilation.casefold() in {"1", "true", "yes", "y"}
        ]

    covers = []
    for picture in src.pictures:
        mime = (picture.mime or "").lower()
        if mime in {"image/jpeg", "image/jpg"} or picture.data.startswith(b"\xff\xd8"):
            covers.append(
                MP4Cover(picture.data, imageformat=MP4Cover.FORMAT_JPEG)
            )
        elif mime == "image/png" or picture.data.startswith(b"\x89PNG"):
            covers.append(
                MP4Cover(picture.data, imageformat=MP4Cover.FORMAT_PNG)
            )
    if covers:
        tags["covr"] = covers

    lyric = (
        clean_lrc(lrc.text)
        if lrc
        else clean_lrc(first(source_tags, "lyrics", "unsyncedlyrics"))
    )
    if lyric:
        tags["\xa9lyr"] = [lyric]

    # Preserve unrecognized Vorbis comments as Apple freeform tags.
    for source_key, values in source_tags.items():
        if source_key in KNOWN or not values:
            continue
        safe_key = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", source_key.upper()
        )[:120] or "UNKNOWN"
        tags[f"----:com.apple.iTunes:FLAC_{safe_key}"] = [
            MP4FreeForm(
                value.encode("utf-8"),
                dataformat=AtomDataType.UTF8,
            )
            for value in values
        ]

    dst.save()


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def ffmpeg_default() -> str:
    if getattr(sys, "frozen", False):
        beside_exe = Path(sys.executable).resolve().parent / "ffmpeg.exe"
        if beside_exe.exists():
            return str(beside_exe)
    return shutil.which("ffmpeg") or "ffmpeg"


def output_path(row: Row, outroot: Path) -> Path:
    return outroot / (
        row.output_name
        or (safe_output_stem(row.flac.path.stem) + ".m4a")
    )


def _run_ffmpeg(
    cmd: list[str], cancel_event: threading.Event | None
) -> tuple[str, str]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )

    while True:
        try:
            _, stderr = process.communicate(timeout=0.20)
            if process.returncode != 0:
                raise RuntimeError((stderr or "FFmpeg 실패").strip())
            return "completed", stderr
        except subprocess.TimeoutExpired:
            if cancel_event and cancel_event.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                return "cancelled", ""


def convert_one(
    row: Row,
    outroot: Path,
    ffmpeg: str,
    overwrite: bool,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str, Path]:
    out = output_path(row, outroot)
    outroot.mkdir(parents=True, exist_ok=True)

    if cancel_event and cancel_event.is_set():
        return "cancelled", "취소됨", out
    if out.exists() and not overwrite:
        return "skipped", "기존 파일", out

    fd, tmp_name = tempfile.mkstemp(
        prefix=".__aac_", suffix=".m4a", dir=outroot
    )
    os.close(fd)
    tmp = Path(tmp_name)

    try:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(row.flac.path),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "320k",
            "-threads",
            "1",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(tmp),
        ]

        state, _ = _run_ffmpeg(cmd, cancel_event)
        if state == "cancelled":
            return "cancelled", "취소됨", out

        if cancel_event and cancel_event.is_set():
            return "cancelled", "취소됨", out

        copy_meta(row.flac, tmp, row.lrc if row.status == "가사 있음" else None)

        if cancel_event and cancel_event.is_set():
            return "cancelled", "취소됨", out

        if out.exists():
            out.unlink()
        shutil.move(str(tmp), str(out))
        return "converted", "완료", out
    except Exception as exc:
        return "failed", str(exc), out
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


class ScanThread(QThread):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(object, object, int)
    fail = pyqtSignal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self):
        try:
            rows, lrcs, duplicates = scan_library(
                self.root,
                lambda a, b, c: self.progress.emit(a, b, c),
            )
            self.done.emit(rows, lrcs, duplicates)
        except Exception as exc:
            self.fail.emit(str(exc))


class ConvertThread(QThread):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(int, int, int, int)
    fail = pyqtSignal(str)

    def __init__(
        self,
        rows: list[Row],
        outroot: Path,
        ffmpeg: str,
        workers: int,
        overwrite: bool,
    ):
        super().__init__()
        self.rows = rows
        self.outroot = outroot
        self.ffmpeg = ffmpeg
        self.workers = workers
        self.overwrite = overwrite
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def run(self):
        converted = skipped = failed = cancelled = done = 0
        total = len(self.rows)

        def job(row: Row):
            return convert_one(
                row,
                self.outroot,
                self.ffmpeg,
                self.overwrite,
                self.cancelled,
            )

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {
                    executor.submit(job, row): row
                    for row in self.rows
                }
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        state, _, _ = future.result()
                    except Exception:
                        state = "failed"

                    if state == "converted":
                        converted += 1
                    elif state == "skipped":
                        skipped += 1
                    elif state == "cancelled":
                        cancelled += 1
                    else:
                        failed += 1

                    done += 1
                    self.progress.emit(done, total, row.flac.label)

            self.done.emit(converted, skipped, failed, cancelled)
        except Exception as exc:
            self.fail.emit(str(exc))


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.rows: list[Row] = []
        self.lrcs: list[LrcItem] = []
        self.duplicate_count = 0
        self.scan_thread: ScanThread | None = None
        self.conv_thread: ConvertThread | None = None

        self.setWindowTitle(APP)
        self.resize(1120, 760)

        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setSpacing(12)

        title = QLabel("FLAC → Apple Music AAC")
        title.setStyleSheet("font-size:24px;font-weight:700")
        layout.addWidget(title)

        subtitle = QLabel(
            "루트 전체를 빠르게 검색하고 중복 곡을 하나만 남긴 뒤, "
            "모든 M4A를 출력 폴더 하나에 AAC-LC 320 kbps로 변환합니다."
        )
        subtitle.setStyleSheet("color:#6b7280")
        layout.addWidget(subtitle)

        grid = QGridLayout()
        layout.addLayout(grid)

        self.root = QLineEdit()
        self.out = QLineEdit()
        self.ff = QLineEdit(ffmpeg_default())

        for row, (label, edit, callback) in enumerate(
            (
                ("음악 루트", self.root, self.browse_root),
                ("출력 폴더", self.out, self.browse_out),
                ("FFmpeg", self.ff, self.browse_ff),
            )
        ):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            button = QPushButton("찾아보기…")
            button.clicked.connect(callback)
            grid.addWidget(button, row, 2)

        counters = QHBoxLayout()
        self.scan = QPushButton("음악 검색")
        self.scan.clicked.connect(self.do_scan)
        counters.addWidget(self.scan)

        self.c1 = QLabel("FLAC 0")
        self.c2 = QLabel("가사 있음 0")
        self.c3 = QLabel("가사 없음 0")
        self.cdup = QLabel("중복 제외 0")
        for label in (self.c1, self.c2, self.c3, self.cdup):
            counters.addWidget(label)
        counters.addStretch()
        layout.addLayout(counters)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["상태", "곡", "FLAC", "LRC"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.manual_lrc)
        layout.addWidget(self.table, 1)

        options = QHBoxLayout()
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(max(1, min(8, (os.cpu_count() or 4) // 2)))
        self.overwrite = QCheckBox("기존 M4A 덮어쓰기")
        options.addWidget(QLabel("병렬 인코딩"))
        options.addWidget(self.workers)
        options.addWidget(QLabel("출력: 한 폴더에 모으기"))
        options.addWidget(self.overwrite)
        options.addStretch()
        layout.addLayout(options)

        actions = QHBoxLayout()
        self.convert = QPushButton("변환 시작")
        self.convert.clicked.connect(self.do_convert)
        self.cancel = QPushButton("취소")
        self.cancel.clicked.connect(self.do_cancel)
        self.cancel.setEnabled(False)
        self.open = QPushButton("출력 폴더 열기")
        self.open.clicked.connect(self.open_out)
        actions.addWidget(self.convert)
        actions.addWidget(self.cancel)
        actions.addStretch()
        actions.addWidget(self.open)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.status = QLabel("준비됨")
        self.status.setStyleSheet("color:#6b7280")
        layout.addWidget(self.progress)
        layout.addWidget(self.status)

        self.setStyleSheet(
            "QMainWindow{background:#f7f8fa}"
            " QWidget{font-size:14px}"
            " QLineEdit,QTableWidget,QSpinBox{background:white;"
            " border:1px solid #d8dce3;border-radius:7px;padding:6px}"
            " QPushButton{padding:8px 14px;border:1px solid #cfd4dc;"
            " border-radius:8px;background:white}"
            " QPushButton:hover{background:#eef2f7}"
            " QPushButton:disabled{color:#aaa;background:#eee}"
        )

    def browse_root(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "음악 루트 선택",
            self.root.text() or str(Path.home()),
        )
        if path:
            self.root.setText(path)
            if not self.out.text():
                root = Path(path)
                self.out.setText(str(root.with_name(root.name + "_AAC")))

    def browse_out(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "출력 폴더 선택",
            self.out.text() or str(Path.home()),
        )
        if path:
            self.out.setText(path)

    def browse_ff(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "ffmpeg.exe 선택",
            str(Path.home()),
            "ffmpeg (ffmpeg.exe);;Executable (*.exe);;All files (*)",
        )
        if path:
            self.ff.setText(path)

    def set_idle(self):
        self.scan.setEnabled(True)
        self.convert.setEnabled(True)
        self.cancel.setEnabled(False)

    def set_scanning(self):
        self.scan.setEnabled(False)
        self.convert.setEnabled(False)
        self.cancel.setEnabled(False)

    def set_converting(self):
        self.scan.setEnabled(False)
        self.convert.setEnabled(False)
        self.cancel.setEnabled(True)

    def do_scan(self):
        root = Path(self.root.text().strip())
        if not root.is_dir():
            QMessageBox.warning(self, "확인", "음악 루트 폴더를 선택해줘.")
            return

        self.set_scanning()
        self.status.setText("검색 중…")
        self.progress.setValue(0)
        self.scan_thread = ScanThread(root)
        self.scan_thread.progress.connect(self.scan_prog)
        self.scan_thread.done.connect(self.scan_done)
        self.scan_thread.fail.connect(self.fail)
        self.scan_thread.start()

    def scan_prog(self, current: int, total: int, name: str):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(current)
        self.status.setText(f"검색 중 · {name}")

    def scan_done(self, rows, lrcs, duplicate_count: int):
        self.rows = list(rows)
        self.lrcs = list(lrcs)
        self.duplicate_count = int(duplicate_count)
        self.set_idle()
        self.refresh()
        self.progress.setValue(self.progress.maximum())
        self.status.setText(
            f"검색 완료 · 중복 {self.duplicate_count}개 제외"
        )

    def refresh(self):
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(self.rows))
            found = missing = 0

            for i, row in enumerate(self.rows):
                if row.status == "가사 있음":
                    found += 1
                    color = QColor("#0f9d58")
                else:
                    missing += 1
                    color = QColor("#6b7280")

                values = [
                    row.status,
                    row.flac.label,
                    str(row.flac.path),
                    str(row.lrc.path) if row.lrc else "",
                ]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(value))
                self.table.item(i, 0).setForeground(color)

            self.c1.setText(f"FLAC {len(self.rows)}")
            self.c2.setText(f"가사 있음 {found}")
            self.c3.setText(f"가사 없음 {missing}")
            self.cdup.setText(f"중복 제외 {self.duplicate_count}")
        finally:
            self.table.setUpdatesEnabled(True)

    def manual_lrc(self, row_index: int, _column: int):
        if row_index < 0 or row_index >= len(self.rows):
            return

        row = self.rows[row_index]
        path, _ = QFileDialog.getOpenFileName(
            self,
            "LRC 직접 변경",
            str(row.flac.path.parent),
            "LRC (*.lrc);;All files (*)",
        )
        if not path:
            return

        try:
            row.lrc = parse_lrc(Path(path))
            row.status = "가사 있음"
            QTimer.singleShot(0, self.refresh)
        except Exception as exc:
            QMessageBox.critical(self, "LRC 오류", str(exc))

    def do_convert(self):
        if not self.rows:
            QMessageBox.warning(self, "확인", "먼저 음악 검색을 해줘.")
            return

        if not self.out.text().strip():
            QMessageBox.warning(self, "확인", "출력 폴더를 선택해줘.")
            return

        out = Path(self.out.text().strip())
        ffmpeg = self.ff.text().strip() or "ffmpeg"

        try:
            check = subprocess.run(
                [ffmpeg, "-version"],
                capture_output=True,
                timeout=8,
                **hidden_subprocess_kwargs(),
            )
            if check.returncode != 0:
                raise RuntimeError()
        except Exception:
            QMessageBox.warning(
                self,
                "FFmpeg",
                "FFmpeg를 실행할 수 없어. FFmpeg 찾아보기에서 "
                "ffmpeg.exe를 지정해줘.",
            )
            return

        out.mkdir(parents=True, exist_ok=True)
        self.set_converting()
        self.status.setText("변환 중…")
        self.progress.setMaximum(len(self.rows))
        self.progress.setValue(0)

        self.conv_thread = ConvertThread(
            self.rows,
            out,
            ffmpeg,
            self.workers.value(),
            self.overwrite.isChecked(),
        )
        self.conv_thread.progress.connect(self.conv_prog)
        self.conv_thread.done.connect(self.conv_done)
        self.conv_thread.fail.connect(self.fail)
        self.conv_thread.start()

    def conv_prog(self, current: int, total: int, name: str):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(current)
        self.status.setText(f"{current}/{total} · {name}")

    def conv_done(
        self,
        converted: int,
        skipped: int,
        failed: int,
        cancelled: int,
    ):
        self.set_idle()
        self.status.setText("완료")
        QMessageBox.information(
            self,
            "완료",
            "변환 완료\n"
            f"변환: {converted}\n"
            f"기존 파일 건너뜀: {skipped}\n"
            f"실패: {failed}\n"
            f"취소: {cancelled}",
        )

    def do_cancel(self):
        if self.conv_thread:
            self.conv_thread.cancel()
            self.cancel.setEnabled(False)
            self.status.setText("취소 중…")

    def open_out(self):
        path = Path(self.out.text().strip())
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def fail(self, message: str):
        self.set_idle()
        QMessageBox.critical(self, "오류", message)
        self.status.setText("오류")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    window = Window()
    window.show()

    if "--smoke-test" in sys.argv:
        QTimer.singleShot(700, app.quit)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

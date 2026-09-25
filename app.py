from __future__ import annotations

import os, re, shutil, subprocess, sys, tempfile, threading, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, AtomDataType
from PyQt6.QtCore import QThread, Qt, pyqtSignal, QUrl, QTimer
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
)

APP = "FLAC → Apple Music AAC"
META_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9_-]*):(.*?)\]\s*$")
TS_RE = re.compile(r"\[(?:\d{1,3}:)?\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\]|\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
WORD_TS_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
NONWORD_RE = re.compile(r"[^0-9a-z가-힣]+", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", s.casefold().strip())


def key(s: str) -> str:
    return NONWORD_RE.sub("", norm(s))


def join_artist_values(values) -> str:
    """여러 artist 계열 값을 '; ' 구분자로 일관되게 표시한다."""
    parts = []
    for value in values or []:
        for part in re.split(r"\s*;\s*", str(value)):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    return "; ".join(parts)


def inferred_title(path: Path) -> str:
    stem = norm(path.stem)
    parts = re.split(r"\s+-\s+", stem, maxsplit=1)
    return parts[1] if len(parts) == 2 and parts[1].strip() else stem


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def parse_lrc(path: Path):
    text = read_text(path)
    tags = {}
    for line in text.splitlines():
        m = META_RE.match(line)
        if m and m.group(1).lower() not in tags:
            tags[m.group(1).lower()] = m.group(2).strip()
    title = tags.get("ti", "") or inferred_title(path)
    return LrcItem(path, title, tags.get("ar", ""), text)


def clean_lrc(text: str) -> str:
    out, blank = [], False
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
        out.append(line); blank = False
    while out and not out[-1]: out.pop()
    return "\n".join(out).strip()


@dataclass
class FlacItem:
    path: Path
    title: str = ""
    artist: str = ""
    tags: dict[str, list[str]] = field(default_factory=dict)
    quality_score: tuple = field(default_factory=tuple)

    @property
    def label(self):
        return f"{self.artist} - {self.title}" if self.artist and self.title else self.path.stem


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
    candidates: list[LrcItem] = field(default_factory=list)
    output_name: str = ""


def load_flac(path: Path) -> FlacItem:
    try:
        a = FLAC(path)
        tags = {k.lower(): [str(v) for v in vals] for k, vals in (a.tags.items() if a.tags else [])}
        firstv = lambda n: (tags.get(n, [""])[0] if tags.get(n) else "").strip()
        artist = join_artist_values(tags.get("artist", []))
        # 중복일 때 더 좋은 원본을 남기기 위한 점수:
        # 1) 앨범아트 존재, 2) 태그 수, 3) 비트심도, 4) 샘플레이트, 5) 파일 크기
        has_art = 1 if getattr(a, "pictures", None) else 0
        tag_count = sum(1 for vals in tags.values() if any(str(v).strip() for v in vals))
        bits = int(getattr(a.info, "bits_per_sample", 0) or 0)
        sr = int(getattr(a.info, "sample_rate", 0) or 0)
        size = path.stat().st_size if path.exists() else 0
        score = (has_art, tag_count, bits, sr, size)
        return FlacItem(path, firstv("title") or inferred_title(path), artist, tags, score)
    except Exception:
        size = path.stat().st_size if path.exists() else 0
        return FlacItem(path, inferred_title(path), "", {}, (0, 0, 0, 0, size))


def dedup_key(f: FlacItem) -> str:
    # Artist가 있으면 Artist+Title, 없으면 Title만으로 묶는다.
    t = key(f.title or inferred_title(f.path))
    a = key(f.artist)
    return f"{a}::{t}" if a else f"::{t}"


def deduplicate_flacs(flacs: list[FlacItem]) -> tuple[list[FlacItem], int]:
    groups: dict[str, list[FlacItem]] = {}
    unique_no_key: list[FlacItem] = []
    for f in flacs:
        k = dedup_key(f)
        if not k or k == "::":
            unique_no_key.append(f)
            continue
        groups.setdefault(k, []).append(f)

    kept = list(unique_no_key)
    removed = 0
    for group in groups.values():
        # 점수가 높은 파일 우선, 동점이면 경로가 짧고 사전순으로 앞선 파일.
        best = sorted(
            group,
            key=lambda x: (
                tuple(-(v if isinstance(v, int) else 0) for v in x.quality_score),
                len(str(x.path)),
                str(x.path).casefold(),
            )
        )[0]
        kept.append(best)
        removed += max(0, len(group) - 1)

    kept.sort(key=lambda x: str(x.path).casefold())
    return kept, removed

WINDOWS_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_output_stem(text: str) -> str:
    s = WINDOWS_BAD_NAME_RE.sub("_", unicodedata.normalize("NFKC", text or "")).strip().rstrip(". ")
    if not s:
        s = "Untitled"
    if s.upper() in WINDOWS_RESERVED_NAMES:
        s = "_" + s
    return s[:180].rstrip(". ") or "Untitled"


def assign_flat_output_names(rows: list[Row]) -> None:
    """모든 M4A를 output 루트 하나에 저장하되 파일명 충돌은 번호로 피한다."""
    used: set[str] = set()
    for r in rows:
        if r.flac.artist and r.flac.title:
            base = f"{r.flac.artist} - {r.flac.title}"
        elif r.flac.title:
            base = r.flac.title
        else:
            base = r.flac.path.stem
        base = safe_output_stem(base)
        name = f"{base}.m4a"
        n = 2
        while name.casefold() in used:
            name = f"{base} ({n}).m4a"
            n += 1
        used.add(name.casefold())
        r.output_name = name


def scan_library(root: Path, progress=None) -> tuple[list[Row], list[LrcItem]]:
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() in {".flac", ".lrc"}]
    fp = sorted((p for p in files if p.suffix.casefold() == ".flac"), key=lambda p: str(p).casefold())
    lp = sorted((p for p in files if p.suffix.casefold() == ".lrc"), key=lambda p: str(p).casefold())
    total, done = len(fp) + len(lp), 0
    flacs, lrcs = [], []
    for p in fp:
        flacs.append(load_flac(p)); done += 1
        if progress: progress(done, total, p.name)
    for p in lp:
        try: lrcs.append(parse_lrc(p))
        except Exception: lrcs.append(LrcItem(p, inferred_title(p), "", ""))
        done += 1
        if progress: progress(done, total, p.name)

    flacs, duplicate_count = deduplicate_flacs(flacs)

    idx: dict[str, list[LrcItem]] = {}
    for l in lrcs:
        k = key(l.title)
        if k: idx.setdefault(k, []).append(l)
    rows = []
    for f in flacs:
        cand = idx.get(key(f.title), [])
        if cand:
            # 수동 확인 단계를 없앴으므로 같은 제목 후보가 하나라도 있으면
            # 내부 우선순위로 하나를 결정하고 즉시 가사 있음으로 처리한다.
            exact = [l for l in cand if key(l.path.stem) == key(f.path.stem)]
            same_dir = [l for l in cand if l.path.parent.resolve() == f.path.parent.resolve()]
            same_artist = [l for l in cand if f.artist and l.artist and key(l.artist) == key(f.artist)]
            chosen = (
                exact[0] if exact else
                same_dir[0] if same_dir else
                same_artist[0] if same_artist else
                cand[0]
            )
            rows.append(Row(f, chosen, "가사 있음"))
        else:
            rows.append(Row(f, None, "가사 없음"))
    assign_flat_output_names(rows)
    return rows, lrcs, duplicate_count


STD = {
    "title":"\xa9nam", "album":"\xa9alb", "artist":"\xa9ART", "albumartist":"aART",
    "album artist":"aART", "composer":"\xa9wrt", "date":"\xa9day", "year":"\xa9day",
    "comment":"\xa9cmt", "description":"desc", "grouping":"\xa9grp", "genre":"\xa9gen",
    "copyright":"cprt", "titlesort":"sonm", "title sort":"sonm", "albumsort":"soal",
    "album sort":"soal", "artistsort":"soar", "artist sort":"soar",
    "albumartistsort":"soaa", "album artist sort":"soaa", "composersort":"soco",
    "composer sort":"soco"
}
KNOWN = set(STD) | {"tracknumber","tracktotal","totaltracks","discnumber","disctotal","totaldiscs","bpm","tempo","compilation","lyrics","unsyncedlyrics"}


def first(tags, *names):
    for n in names:
        v = tags.get(n.lower())
        if v: return str(v[0]).strip()
    return ""


def num_total(value, total=""):
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", value or "")
    if not m: return (0, int(total) if str(total).isdigit() else 0)
    return int(m.group(1)), int(m.group(2)) if m.group(2) else (int(total) if str(total).isdigit() else 0)


def copy_meta(src_path: Path, dst_path: Path, lrc: LrcItem | None):
    src, dst = FLAC(src_path), MP4(dst_path)
    if dst.tags is None: dst.add_tags()
    dst.tags.clear(); tags = dst.tags
    st = {k.lower(): [str(v) for v in vals] for k, vals in (src.tags.items() if src.tags else [])}
    seen = set()
    for sk, atom in STD.items():
        if st.get(sk) and atom not in seen:
            if sk in {"artist", "albumartist", "album artist"}:
                joined = join_artist_values(st[sk])
                if joined:
                    tags[atom] = [joined]
            else:
                tags[atom] = st[sk]
            seen.add(atom)
    tr = num_total(first(st,"tracknumber"), first(st,"tracktotal","totaltracks"))
    ds = num_total(first(st,"discnumber"), first(st,"disctotal","totaldiscs"))
    if tr != (0,0): tags["trkn"] = [tr]
    if ds != (0,0): tags["disk"] = [ds]
    bpm = first(st,"bpm","tempo")
    if bpm:
        try: tags["tmpo"] = [max(0,min(65535,int(round(float(bpm)))))]
        except ValueError: pass
    comp = first(st,"compilation")
    if comp: tags["cpil"] = [comp.casefold() in {"1","true","yes","y"}]
    covers = []
    for p in src.pictures:
        if (p.mime or "").lower() in {"image/jpeg","image/jpg"} or p.data.startswith(b"\xff\xd8"):
            covers.append(MP4Cover(p.data, imageformat=MP4Cover.FORMAT_JPEG))
        elif (p.mime or "").lower() == "image/png" or p.data.startswith(b"\x89PNG"):
            covers.append(MP4Cover(p.data, imageformat=MP4Cover.FORMAT_PNG))
    if covers: tags["covr"] = covers
    lyric = clean_lrc(lrc.text) if lrc else clean_lrc(first(st,"lyrics","unsyncedlyrics"))
    if lyric: tags["\xa9lyr"] = [lyric]
    for k, vals in st.items():
        if k in KNOWN or not vals: continue
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", k.upper())[:120] or "UNKNOWN"
        tags[f"----:com.apple.iTunes:FLAC_{safe}"] = [MP4FreeForm(v.encode("utf-8"), dataformat=AtomDataType.UTF8) for v in vals]
    dst.save()


def hidden_subprocess_kwargs() -> dict:
    """Windows에서 FFmpeg 같은 콘솔 프로그램을 창 없이 실행한다."""
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
        p = Path(sys.executable).resolve().parent / "ffmpeg.exe"
        if p.exists(): return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


def output_path(row: Row, outroot: Path) -> Path:
    # 앨범/원본 폴더 구조를 만들지 않고 output 폴더 하나에만 저장한다.
    return outroot / (row.output_name or (safe_output_stem(row.flac.path.stem) + ".m4a"))


def convert_one(row: Row, outroot: Path, ffmpeg: str, overwrite: bool):
    out = output_path(row, outroot); outroot.mkdir(parents=True, exist_ok=True)
    if out.exists() and not overwrite: return True, "건너뜀", out
    fd, tmpname = tempfile.mkstemp(prefix=".__aac_", suffix=".m4a", dir=out.parent); os.close(fd); tmp = Path(tmpname)
    try:
        cmd = [ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-y","-i",str(row.flac.path),"-map","0:a:0","-vn","-c:a","aac","-profile:a","aac_low","-b:a","320k","-threads","1","-map_metadata","-1","-movflags","+faststart",str(tmp)]
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        if p.returncode != 0: raise RuntimeError((p.stderr or "FFmpeg 실패").strip())
        copy_meta(row.flac.path, tmp, row.lrc if row.status == "가사 있음" else None)
        if out.exists(): out.unlink()
        shutil.move(str(tmp), str(out)); return True, "완료", out
    except Exception as e:
        return False, str(e), out
    finally:
        try:
            if tmp.exists(): tmp.unlink()
        except Exception: pass


class ScanThread(QThread):
    progress = pyqtSignal(int,int,str); done = pyqtSignal(object,object,int); fail = pyqtSignal(str)
    def __init__(self, root): super().__init__(); self.root = root
    def run(self):
        try:
            r,l,d = scan_library(self.root, lambda a,b,c:self.progress.emit(a,b,c)); self.done.emit(r,l,d)
        except Exception as e: self.fail.emit(str(e))


class ConvertThread(QThread):
    progress = pyqtSignal(int,int,str); done = pyqtSignal(int,int,int); fail = pyqtSignal(str)
    def __init__(self, rows, outroot, ffmpeg, workers, overwrite):
        super().__init__(); self.rows=rows; self.outroot=outroot; self.ffmpeg=ffmpeg; self.workers=workers; self.overwrite=overwrite; self.cancelled=threading.Event()
    def cancel(self): self.cancelled.set()
    def run(self):
        ok=bad=skip=done=0; total=len(self.rows)
        def job(r):
            if self.cancelled.is_set(): return None
            return convert_one(r,self.outroot,self.ffmpeg,self.overwrite)
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs={ex.submit(job,r):r for r in self.rows}
                for f in as_completed(futs):
                    r=futs[f]
                    try: res=f.result()
                    except Exception as e: res=(False,str(e),None)
                    if res is None: skip += 1
                    elif res[0]: ok += 1
                    else: bad += 1
                    done += 1; self.progress.emit(done,total,r.flac.label)
            self.done.emit(ok,bad,skip)
        except Exception as e: self.fail.emit(str(e))


class Window(QMainWindow):
    def __init__(self):
        super().__init__(); self.rows=[]; self.lrcs=[]; self.duplicate_count=0; self.scan_thread=None; self.conv_thread=None
        self.setWindowTitle(APP); self.resize(1120,760)
        w=QWidget(); self.setCentralWidget(w); v=QVBoxLayout(w); v.setSpacing(12)
        title=QLabel("FLAC → Apple Music AAC"); title.setStyleSheet("font-size:24px;font-weight:700"); v.addWidget(title)
        sub=QLabel("루트 전체를 검색하고 중복 곡은 하나만 남긴 뒤, 모든 M4A를 출력 폴더 하나에 모아 AAC-LC 320 kbps로 병렬 변환합니다."); sub.setStyleSheet("color:#6b7280"); v.addWidget(sub)
        grid=QGridLayout(); v.addLayout(grid)
        self.root=QLineEdit(); self.out=QLineEdit(); self.ff=QLineEdit(ffmpeg_default())
        for row,(lab,edit,fn) in enumerate((("음악 루트",self.root,self.browse_root),("출력 폴더",self.out,self.browse_out),("FFmpeg",self.ff,self.browse_ff))):
            grid.addWidget(QLabel(lab),row,0); grid.addWidget(edit,row,1); b=QPushButton("찾아보기…"); b.clicked.connect(fn); grid.addWidget(b,row,2)
        h=QHBoxLayout(); self.scan=QPushButton("음악 검색"); self.scan.clicked.connect(self.do_scan); h.addWidget(self.scan)
        self.c1=QLabel("FLAC 0"); self.c2=QLabel("가사 있음 0"); self.c3=QLabel("가사 없음 0"); self.cdup=QLabel("중복 제외 0")
        for x in (self.c1,self.c2,self.c3,self.cdup): h.addWidget(x)
        h.addStretch(); v.addLayout(h)
        self.table=QTableWidget(0,4); self.table.setHorizontalHeaderLabels(["상태","곡","FLAC","LRC"]); self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.ResizeToContents); self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents); self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch); self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch); self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.table.cellDoubleClicked.connect(self.manual_lrc); v.addWidget(self.table,1)
        opts=QHBoxLayout(); self.workers=QSpinBox(); self.workers.setRange(1,16); self.workers.setValue(max(1,min(8,(os.cpu_count() or 4)//2))); self.over=QCheckBox("기존 M4A 덮어쓰기"); opts.addWidget(QLabel("병렬 인코딩")); opts.addWidget(self.workers); opts.addWidget(QLabel("출력: 한 폴더에 모으기")); opts.addWidget(self.over); opts.addStretch(); v.addLayout(opts)
        h2=QHBoxLayout(); self.convert=QPushButton("변환 시작"); self.convert.clicked.connect(self.do_convert); self.cancel=QPushButton("취소"); self.cancel.clicked.connect(self.do_cancel); self.cancel.setEnabled(False); self.open=QPushButton("출력 폴더 열기"); self.open.clicked.connect(self.open_out); h2.addWidget(self.convert); h2.addWidget(self.cancel); h2.addStretch(); h2.addWidget(self.open); v.addLayout(h2)
        self.progress=QProgressBar(); self.status=QLabel("준비됨"); self.status.setStyleSheet("color:#6b7280"); v.addWidget(self.progress); v.addWidget(self.status)
        self.setStyleSheet("QMainWindow{background:#f7f8fa} QWidget{font-size:14px} QLineEdit,QTableWidget,QSpinBox{background:white;border:1px solid #d8dce3;border-radius:7px;padding:6px} QPushButton{padding:8px 14px;border:1px solid #cfd4dc;border-radius:8px;background:white} QPushButton:hover{background:#eef2f7} QPushButton:disabled{color:#aaa;background:#eee}")

    def browse_root(self):
        p=QFileDialog.getExistingDirectory(self,"음악 루트 선택",self.root.text() or str(Path.home()))
        if p:
            self.root.setText(p)
            if not self.out.text(): self.out.setText(str(Path(p).with_name(Path(p).name+"_AAC")))
    def browse_out(self):
        p=QFileDialog.getExistingDirectory(self,"출력 폴더 선택",self.out.text() or str(Path.home()))
        if p: self.out.setText(p)
    def browse_ff(self):
        p,_=QFileDialog.getOpenFileName(self,"ffmpeg.exe 선택",str(Path.home()),"ffmpeg (ffmpeg.exe);;Executable (*.exe);;All files (*)")
        if p: self.ff.setText(p)
    def set_busy(self,b): self.scan.setEnabled(not b); self.convert.setEnabled(not b); self.cancel.setEnabled(b)

    def do_scan(self):
        p=Path(self.root.text().strip())
        if not p.is_dir(): QMessageBox.warning(self,"확인","음악 루트 폴더를 선택해줘."); return
        self.set_busy(True); self.status.setText("검색 중…"); self.progress.setValue(0)
        self.scan_thread=ScanThread(p); self.scan_thread.progress.connect(self.scan_prog); self.scan_thread.done.connect(self.scan_done); self.scan_thread.fail.connect(self.fail); self.scan_thread.start()
    def scan_prog(self,a,b,s): self.progress.setMaximum(max(1,b)); self.progress.setValue(a); self.status.setText(f"검색 중 · {s}")
    def scan_done(self,rows,lrcs,duplicate_count):
        self.rows=list(rows); self.lrcs=list(lrcs); self.duplicate_count=int(duplicate_count); self.set_busy(False); self.refresh(); self.progress.setValue(self.progress.maximum()); self.status.setText(f"검색 완료 · 중복 {self.duplicate_count}개 제외")
    def refresh(self):
        self.table.setRowCount(len(self.rows)); found=missing=0
        for i,r in enumerate(self.rows):
            if r.status=="가사 있음":
                found+=1; color=QColor("#0f9d58")
            else:
                missing+=1; color=QColor("#6b7280")
            vals=[r.status,r.flac.label,str(r.flac.path),str(r.lrc.path) if r.lrc else ""]
            for j,x in enumerate(vals):
                it=QTableWidgetItem(x); self.table.setItem(i,j,it)
            self.table.item(i,0).setForeground(color)
        self.c1.setText(f"FLAC {len(self.rows)}"); self.c2.setText(f"가사 있음 {found}"); self.c3.setText(f"가사 없음 {missing}"); self.cdup.setText(f"중복 제외 {self.duplicate_count}")
    def manual_lrc(self,row,col):
        if row<0 or row>=len(self.rows): return
        r = self.rows[row]
        start = str(r.flac.path.parent)
        p,_ = QFileDialog.getOpenFileName(
            self,
            "LRC 직접 변경",
            start,
            "LRC (*.lrc);;All files (*)"
        )
        if not p:
            return
        try:
            l = parse_lrc(Path(p))
            r.lrc = l
            r.status = "가사 있음"
            r.candidates = []
            QTimer.singleShot(0, self.refresh)
        except Exception as e:
            QMessageBox.critical(self,"LRC 오류",str(e))

    def do_convert(self):
        if not self.rows: QMessageBox.warning(self,"확인","먼저 음악 검색을 해줘."); return
        out=Path(self.out.text().strip())
        if not self.out.text().strip(): QMessageBox.warning(self,"확인","출력 폴더를 선택해줘."); return
        ff=self.ff.text().strip() or "ffmpeg"
        try:
            p=subprocess.run(
                [ff,"-version"],
                capture_output=True,
                timeout=8,
                **hidden_subprocess_kwargs(),
            )
            if p.returncode!=0: raise RuntimeError()
        except Exception:
            QMessageBox.warning(self,"FFmpeg","FFmpeg를 실행할 수 없어. FFmpeg 찾아보기에서 ffmpeg.exe를 지정해줘."); return
        out.mkdir(parents=True,exist_ok=True); self.set_busy(True); self.status.setText("변환 중…"); self.progress.setMaximum(len(self.rows)); self.progress.setValue(0)
        self.conv_thread=ConvertThread(self.rows,out,ff,self.workers.value(),self.over.isChecked()); self.conv_thread.progress.connect(self.conv_prog); self.conv_thread.done.connect(self.conv_done); self.conv_thread.fail.connect(self.fail); self.conv_thread.start()
    def conv_prog(self,a,b,s): self.progress.setMaximum(max(1,b)); self.progress.setValue(a); self.status.setText(f"{a}/{b} · {s}")
    def conv_done(self,ok,bad,skip): self.set_busy(False); self.status.setText("완료"); QMessageBox.information(self,"완료",f"변환 완료\n성공/건너뜀: {ok}\n실패: {bad}\n취소: {skip}")
    def do_cancel(self):
        if self.conv_thread: self.conv_thread.cancel(); self.status.setText("취소 요청됨…")
    def open_out(self):
        p=Path(self.out.text().strip())
        if p.exists(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
    def fail(self,msg): self.set_busy(False); QMessageBox.critical(self,"오류",msg); self.status.setText("오류")


def main():
    app=QApplication(sys.argv); app.setApplicationName(APP); win=Window(); win.show()
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(700, app.quit)
    sys.exit(app.exec())

if __name__=="__main__": main()

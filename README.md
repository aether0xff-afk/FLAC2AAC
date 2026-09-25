# FLAC2AAC

Windows용 **FLAC → Apple Music/iPad AAC 변환 GUI**입니다.

## 주요 기능

- FLAC를 **AAC-LC 320 kbps (.m4a)** 로 변환
- LRC를 루트 전체에서 찾아 **제목 기준으로 자동 매칭**
- LRC 타임스탬프/메타 태그를 제거하고 M4A의 `©lyr` 가사 태그에 임베딩
- FLAC 메타데이터와 앨범아트 유지
  - Title / Artist / Album / Album Artist
  - Track / Disc
  - Genre / Date / Composer / BPM 등
  - Cover artwork
- 중복 곡은 변환 목록에서 하나만 유지
- 모든 결과 파일을 선택한 **output 폴더 하나에 평평하게 저장**
- 결과 파일명: `Artist - Title.m4a`
- 이름 충돌 시 `Artist - Title (2).m4a` 형식으로 자동 회피
- 병렬 변환 지원
- FFmpeg 실행 시 CMD 창 숨김
- 원본 FLAC/LRC는 수정하거나 삭제하지 않음

## LRC 매칭

같은 제목의 LRC가 하나라도 있으면 자동 적용합니다.

후보가 여러 개일 때 내부 우선순위:

1. FLAC/LRC 파일명 일치
2. 같은 폴더
3. Artist 일치
4. 첫 번째 후보

수동 확인 단계는 없습니다.

## 중복 처리

기본 중복 키:

- Artist가 있으면: `Artist + Title`
- Artist가 없으면: `Title`

중복 중 남길 파일 우선순위:

1. 앨범아트 있음
2. 메타데이터가 더 많음
3. 더 높은 bit depth
4. 더 높은 sample rate
5. 더 큰 파일

원본 파일은 삭제하지 않고 **변환 대상에서만 제외**합니다.

## 실행

Releases의 Windows standalone EXE를 사용하면 Python/PyQt 설치가 필요 없습니다.

FFmpeg는 별도로 필요합니다.

- `ffmpeg.exe`가 PATH에 있으면 자동 감지
- 아니면 앱의 **FFmpeg → 찾아보기**에서 직접 선택

## 소스 실행

```powershell
py -3 -m pip install -r requirements.txt
py -3 app.py
```

## Windows EXE 직접 빌드

`build_exe.bat` 실행:

```text
dist\FLAC2AAC.exe
```

PyInstaller `--onefile --windowed` 빌드이므로 사용자 PC에서 Python이 필요하지 않습니다.

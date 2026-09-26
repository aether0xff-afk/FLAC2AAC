# FLAC2AAC

Windows용 **FLAC → Apple Music/iPad용 AAC(.m4a) 변환 GUI**입니다.  
FLAC 라이브러리와 LRC 가사를 한 번에 스캔해 중복 곡을 정리하고, 가사를 자동 연결한 뒤 모든 결과물을 하나의 output 폴더에 모아 변환합니다.

## 지원 기능

### FLAC 스캔
- 선택한 음악 루트 폴더를 **하위 폴더까지 재귀 검색**
- `.flac` / `.lrc` 파일을 전체 라이브러리에서 함께 탐색
- FLAC의 `TITLE` / `ARTIST` 메타데이터를 우선 사용
- FLAC에 `ARTIST`가 여러 개 있으면 `Artist A, Artist B`처럼 **`, ` 구분자**로 합쳐 GUI/파일명에 사용
- TITLE 또는 ARTIST가 없으면 `Artist - Title.flac` 파일명에서 각각 추론
  - 표시용 제목/아티스트는 원래 대소문자를 보존
  - 구분자가 없으면 파일명 전체를 제목으로 사용
- 메타데이터 파싱을 최대 16 worker로 병렬화해 대형 라이브러리 검색 속도를 개선
- 검색은 별도 QThread에서 실행되어 GUI를 막지 않음
- 검색 진행률과 현재 처리 중인 파일 표시

### LRC 자동 검색 및 매칭
- LRC가 FLAC과 같은 폴더에 없어도 **선택한 루트 전체에서 검색**
- LRC의 `[ti:]` 값을 제목으로 사용
- `[ti:]`가 없으면 LRC 파일명에서 제목 추론
- 제목 비교 시 다음 정규화 적용
  - Unicode NFKC 정규화
  - 대소문자 무시
  - 공백 무시
  - 대부분의 특수문자/구두점 무시
  - 특정 언어에 한정하지 않고 **모든 Unicode 문자/숫자**를 유지
- 같은 제목의 LRC가 하나라도 있으면 **확인 단계 없이 자동 적용**
- 같은 제목 후보가 여러 개면 다음 우선순위로 하나를 선택
  1. FLAC/LRC 파일명 정규화 값 일치
  2. FLAC과 같은 폴더
  3. Artist 일치
  4. 정렬된 후보 중 첫 번째
- **LRC 1:1 전역 배정**: 한 LRC 파일은 자동 매칭에서 최대 한 곡에만 사용
  - 동명곡이 여러 개이고 LRC가 부족하면 우선순위가 높은 곡에 먼저 배정하고 나머지는 `가사 없음` 처리
- 상태는 `가사 있음` / `가사 없음` 두 가지만 사용
- 별도의 `확인 필요` 상태 없음
- 테이블에서 곡을 더블클릭하면 원하는 LRC를 직접 선택해 **수동 교체 가능**

### 지원 LRC 문자 인코딩
다음 순서로 자동 디코딩을 시도합니다.

- UTF-8 with BOM
- UTF-8
- CP949
- EUC-KR
- UTF-16
- UTF-16 LE
- UTF-16 BE
- 모두 실패하면 UTF-8 replacement 모드로 읽음

### LRC 정리 및 M4A 가사 임베딩
LRC를 그대로 넣지 않고 Apple Music/iPad에서 읽기 쉬운 일반 텍스트 가사로 정리합니다.

제거 대상:
- `[ar:]`
- `[ti:]`
- `[al:]`
- `[by:]`
- `[offset:]`
- 그 외 `[key:value]` 형태의 LRC 메타 태그
- `[00:12.34]` 같은 일반 타임스탬프
- `<00:12.34>` 같은 enhanced-LRC word timestamp
- 불필요하게 반복되는 빈 줄

정리된 가사는 M4A의 Apple lyrics atom인 **`©lyr`** 에 저장됩니다.

외부 LRC가 매칭되지 않은 경우 FLAC 자체에 `LYRICS` 또는 `UNSYNCEDLYRICS` 태그가 있으면 그것을 정리해 대신 사용합니다.

### 중복 곡 자동 제거
원본 파일을 삭제하지 않고 **변환 목록에서만 중복을 제거**합니다.

중복 판정:
- Artist가 있으면 → `Artist + Title`
- Artist가 없으면 → `Title`

비교 시 제목/아티스트는 매칭과 같은 방식으로 정규화됩니다.

중복 그룹에서 남길 FLAC 우선순위:
1. 앨범아트가 있는 파일
2. 비어 있지 않은 메타데이터 태그가 더 많은 파일
3. 더 높은 bit depth
4. 더 높은 sample rate
5. 더 큰 파일 크기
6. 완전히 동점이면 더 짧은 경로
7. 그래도 같으면 경로 사전순

GUI에서 `중복 제외 N`으로 제외된 개수를 표시합니다.

### AAC 변환
- 출력 포맷: `.m4a`
- 코덱: **AAC-LC**
- 목표 비트레이트: **320 kbps**
- FFmpeg의 첫 번째 오디오 스트림만 변환
- 영상 스트림은 포함하지 않음
- `faststart` 적용
- FFmpeg 자체는 작업 하나당 1 thread 사용
- 여러 곡은 Python ThreadPoolExecutor로 **병렬 변환**
- 병렬 작업 수: **1~16**
- 기본값: CPU 코어 수의 절반 정도, 최대 8
- 변환 진행률과 현재 곡 표시
- 취소 버튼 지원
  - 아직 시작하지 않은 작업은 즉시 취소 처리
  - **이미 실행 중인 FFmpeg 프로세스도 종료 요청**하고 필요 시 강제 종료
- 완료 창에서 `변환 / 기존 파일 건너뜀 / 실패 / 취소`를 분리 집계

### CMD 창 숨김
Windows에서 FFmpeg 실행 시:
- `CREATE_NO_WINDOW`
- `STARTF_USESHOWWINDOW`
- `SW_HIDE`

를 사용해 변환 중 검은 CMD 창이 뜨거나 깜빡이지 않도록 처리합니다.

FFmpeg 버전 확인 시에도 같은 방식으로 창을 숨깁니다.

### FLAC 메타데이터 유지
FFmpeg의 자동 metadata copy에 의존하지 않고, 변환 후 Mutagen으로 M4A 태그를 다시 구성합니다.

- 원본 `TITLE`/`ARTIST` 태그가 없어도 스캔 단계에서 파일명으로 추론한 값을 M4A `Title`/`Artist`에 보충
- 따라서 파일명에서만 알 수 있었던 제목/아티스트가 변환 후 사라지지 않음

지원하는 표준 태그:
- Title
- Artist
  - 여러 `ARTIST` 값은 `Artist A, Artist B`처럼 `, `로 합쳐 M4A Artist 태그에 저장
- Album
- Album Artist
  - 여러 `ALBUMARTIST` 값도 같은 방식으로 `, ` 구분자를 사용
- Composer
- Date / Year
- Genre
- Comment
- Description
- Grouping
- Copyright
- BPM / Tempo
- Compilation
- Track number / Track total
- Disc number / Disc total
- Title Sort
- Album Sort
- Artist Sort
- Album Artist Sort
- Composer Sort
- Lyrics

앨범아트:
- JPEG
- PNG
- FLAC에 여러 picture block이 있으면 지원 형식의 이미지를 M4A `covr`에 기록

표준 매핑에 없는 FLAC Vorbis Comment도 버리지 않고 가능한 경우 Apple freeform tag로 보존합니다.

형식:
- `----:com.apple.iTunes:FLAC_<TAG>`

따라서 **앨범 폴더를 만들지 않아도 Album / Album Artist / Track / Disc / Artwork 정보는 M4A 내부에 그대로 남습니다.**

### output 폴더 평탄화
앨범/아티스트별 하위 폴더를 만들지 않습니다.

모든 결과 파일은 선택한 output 폴더 바로 아래에 저장됩니다.

예:
```text
output/
  IU - Love wins all.m4a
  QWER - 고민중독.m4a
  DAY6 - 한 페이지가 될 수 있게.m4a
```

기본 파일명:
- Artist와 Title이 있으면 → `Artist - Title.m4a`
- Artist가 여러 명이면 → `Artist A, Artist B - Title.m4a`
- Artist가 없으면 → `Title.m4a`
- Title도 없으면 → 원본 파일명 기반

Windows 파일명 안전 처리:
- `< > : " / \\ | ? *` 및 제어문자를 `_`로 치환
- 끝의 점/공백 제거
- `CON`, `PRN`, `AUX`, `NUL`, `COM1~9`, `LPT1~9` 같은 예약 이름 회피
- 파일명 stem을 최대 180자로 제한

같은 출력 파일명이 생기면:
- `Artist - Song.m4a`
- `Artist - Song (2).m4a`
- `Artist - Song (3).m4a`

처럼 자동으로 번호를 붙입니다.

### 기존 출력 파일 처리
- 기본값: 기존 M4A가 있으면 건너뜀
- `기존 M4A 덮어쓰기` 옵션을 켜면 새 파일로 교체
- 변환은 임시 M4A에 먼저 수행한 뒤 메타데이터 기록이 끝나면 최종 파일로 이동
- 실패 시 임시 파일 정리

### 원본 보호
- 원본 FLAC 삭제 안 함
- 원본 FLAC 수정 안 함
- 원본 LRC 삭제 안 함
- 원본 LRC 수정 안 함
- 중복으로 판단된 파일도 실제 삭제하지 않고 변환 대상에서만 제외

### GUI
PyQt6 기반 Windows GUI입니다.

지원 UI:
- 음악 루트 선택
- output 폴더 선택
- FFmpeg 직접 선택
- 음악 검색
- FLAC 개수 표시
- 가사 있음 개수 표시
- 가사 없음 개수 표시
- 중복 제외 개수 표시
- 상태 / 곡 / FLAC / LRC 경로 테이블
- 곡 더블클릭으로 LRC 수동 교체
- 병렬 인코딩 수 조절
- 기존 M4A 덮어쓰기
- 변환 시작
- 취소
- 진행률 표시
- 출력 폴더 열기

## FFmpeg 감지
FFmpeg는 현재 standalone EXE 내부에 포함하지 않습니다.

탐색 순서:
1. standalone EXE와 같은 폴더의 `ffmpeg.exe`
2. Windows PATH에 등록된 `ffmpeg`
3. 사용자가 GUI에서 직접 선택한 경로

변환 직전에 `ffmpeg -version`으로 실행 가능 여부를 확인합니다.

## Standalone EXE
PyInstaller의:
- `--onefile`
- `--windowed`
- 필요한 PyQt6 모듈을 PyInstaller가 자동 추적하도록 구성해 Windows standalone EXE를 빌드합니다.

standalone EXE에는:
- Python runtime
- PyQt6 / Qt
- Mutagen
- 애플리케이션 코드

가 포함되므로 사용자 PC에 Python/PyQt/Mutagen을 별도로 설치할 필요가 없습니다.

단, **FFmpeg는 별도 필요**합니다.

## 소스 실행

필요 환경:
- Python 3
- PyQt6
- Mutagen
- FFmpeg

```powershell
py -3 -m pip install -r requirements.txt
py -3 app.py
```

## Windows EXE 직접 빌드

```powershell
build_exe.bat
```

또는:

```powershell
py -3 -m pip install -r requirements-dev.txt
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name FLAC2AAC app.py
```

결과:
```text
dist\FLAC2AAC.exe
```

## GitHub Actions
`.github/workflows/build-windows.yml`에서 Windows 빌드를 자동 검증합니다.

현재 워크플로:
- main 브랜치 push 시 실행
- 수동 `workflow_dispatch` 지원
- 동일 release workflow는 최신 실행만 유지해 오래된 빌드가 최신 Release를 덮어쓰지 않게 함
- Python 3.12 + pip cache 사용
- FFmpeg/FFprobe 존재 확인, 없으면 설치
- unit test + **실제 1초 FLAC을 생성해 AAC로 변환하는 end-to-end test** 실행
  - AAC-LC 확인
  - 추론 Title / 다중 Artist / Album Artist / Track / Disc / Genre / Date / BPM / Compilation 확인
  - JPEG/PNG artwork 및 LRC `©lyr` 확인
  - unknown Vorbis comment freeform 보존 확인
  - 기존 출력 skip 및 사전 cancellation 확인
- PyInstaller는 실제 import된 Qt 모듈만 묶어 불필요한 `--collect-all PyQt6`를 제거
- Windows PE / Python runtime / PyQt6 / Mutagen 포함 여부 검증
- 실제 EXE를 `--smoke-test`로 실행해 GUI 시작/종료 확인
- SHA-256 체크섬 파일과 함께 `latest` Release에 게시

## 현재 설계상 의도
- 폴더 구조를 보존하지 않고 output 하나에 모으기
- 가사 후보가 있으면 사용자 확인 없이 자동 적용
- 중복 곡은 하나만 변환
- Apple Music/iPad에서 사용할 수 있는 M4A 메타데이터와 일반 텍스트 가사 생성
- 변환 과정에서 원본 라이브러리는 건드리지 않기


## 실제 음악 라이브러리 기반 smoke fixture

실제 음악 파일 자체를 GitHub에 올리지 않고, **네 음악 폴더의 FLAC 메타데이터 + 실제 LRC 파일만** private fixture로 수집해서 smoke test에 사용할 수 있습니다.

수집되는 것:
- FLAC 상대 경로
- Vorbis Comment 전체 태그
- sample rate / bit depth / channels / duration
- 앨범아트의 MIME / 크기 / SHA-256 같은 특성 정보
- 실제 LRC 파일 원본 바이트
- LRC 인코딩 추정값
- FLAC/LRC 개수와 간단한 통계

수집하지 않는 것:
- FLAC 오디오 본문
- 절대 경로
- 실제 앨범아트 이미지 원본

기본 fixture 경로인 `.smoke-data/`는 `.gitignore`에 포함되어 있어 public repo에 실수로 올라가지 않도록 했습니다.

### 1. 네 음악 폴더에서 fixture 수집

PowerShell에서 repo 루트 기준:

```powershell
py -3 tools/collect_smoke_fixture.py "D:\Music"
```

다른 위치에 저장하려면:

```powershell
py -3 tools/collect_smoke_fixture.py "D:\Music" -o "D:\FLAC2AAC-smoke"
```

생성 예:

```text
.smoke-data/
  manifest.json
  collection-summary.json
  lrc/
    Artist/
      Album/
        Artist - Song.lrc
```

### 2. 실제 fixture로 smoke 실행

```powershell
py -3 tools/run_smoke_fixture.py .smoke-data
```

기본 동작:
- manifest에 기록된 **모든 FLAC 메타데이터를 이용해 0.08초 synthetic FLAC 재생성**
- 실제 LRC 파일은 원래 상대 경로 그대로 복원
- 전체 synthetic library를 FLAC2AAC의 실제 scanner로 검색
- 실제 중복 제거 및 LRC 1:1 매칭 수행
- 다양성이 높은 곡을 기본 30개 선택
- 실제 FFmpeg로 AAC-LC 변환
- 생성된 M4A를 Mutagen/FFprobe로 다시 열어 검증

검증 항목:
- AAC codec / LC profile
- Title / Artist
- 여러 Artist의 comma 표기
- LRC 임베딩 여부
- output 단일 폴더 생성
- 실제 라이브러리의 Unicode/특수문자/인코딩 패턴
- 중복 제거 및 LRC 매칭이 전체 라이브러리 규모에서도 예외 없이 동작하는지

변환 검증 개수 변경:

```powershell
py -3 tools/run_smoke_fixture.py .smoke-data --convert-limit 100
```

완료되면:

```text
.smoke-data/last-smoke-report.json
```

에 결과를 남깁니다.

이 방식은 **실제 음악 라이브러리의 구조와 태그/LRC 특성을 그대로 사용하면서 오디오 원본은 테스트 데이터에 포함하지 않는 것**이 목적입니다.

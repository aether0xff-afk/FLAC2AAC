from pathlib import Path

import app


def test_clean_lrc_removes_tags_and_timestamps():
    text = (
        "[ar:Artist]\n"
        "[ti:Song]\n"
        "[00:01.00]Hello\n"
        "<00:02.00>world\n"
        "\n\n"
        "[00:03.00]Again"
    )
    assert app.clean_lrc(text) == "Hello\nworld\n\nAgain"


def test_filename_inference_preserves_case_and_artist():
    path = Path("Artist Name - Love Story.flac")
    assert app.infer_filename_fields(path) == ("Artist Name", "Love Story")
    assert app.inferred_title(path) == "Love Story"
    assert app.inferred_artist(path) == "Artist Name"


def test_matching_key_keeps_non_latin_letters_and_ignores_diacritics():
    assert app.key("宇多田 ヒカル") == app.key("宇多田ヒカル")
    assert app.key("Beyoncé!") == app.key("Beyonce")


def test_multiple_artists_use_commas_without_semicolons():
    assert app.join_artist_values(["Artist A", "Artist B"]) == "Artist A, Artist B"
    assert app.join_artist_values(["Artist A; Artist B"]) == "Artist A, Artist B"


def test_flat_output_names_and_collision_numbering():
    first = app.Row(
        app.FlacItem(Path("x.flac"), "Song", "Artist"),
        None,
        "가사 없음",
    )
    second = app.Row(
        app.FlacItem(Path("y.flac"), "Song", "Artist"),
        None,
        "가사 없음",
    )
    app.assign_flat_output_names([first, second])
    assert first.output_name == "Artist - Song.m4a"
    assert second.output_name == "Artist - Song (2).m4a"


def test_windows_reserved_output_name_is_escaped():
    assert app.safe_output_stem("CON") == "_CON"
    assert app.safe_output_stem("CON.txt") == "_CON.txt"
    assert app.safe_output_stem('A:B?C') == "A_B_C"


def test_missing_artist_metadata_is_inferred_before_dedup(tmp_path):
    # Dummy bytes intentionally force load_flac() onto filename fallback.
    a = tmp_path / "Artist A - Same Title.flac"
    b = tmp_path / "Artist B - Same Title.flac"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    rows, _, duplicate_count = app.scan_library(tmp_path)

    assert duplicate_count == 0
    assert len(rows) == 2
    assert {row.flac.artist for row in rows} == {"Artist A", "Artist B"}


def test_real_duplicates_collapse_to_one(tmp_path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    (a_dir / "Artist - Song.flac").write_bytes(b"small")
    (b_dir / "Artist - Song.flac").write_bytes(b"a little larger")

    rows, _, duplicate_count = app.scan_library(tmp_path)

    assert duplicate_count == 1
    assert len(rows) == 1
    assert rows[0].flac.title == "Song"
    assert rows[0].flac.artist == "Artist"


def test_lrc_allocation_is_one_to_one(tmp_path):
    # Two different artists with the same title, but only one LRC.
    (tmp_path / "Artist A - Hello.flac").write_bytes(b"a")
    (tmp_path / "Artist B - Hello.flac").write_bytes(b"b")
    (tmp_path / "Hello.lrc").write_text(
        "[ti:Hello]\n[00:01.00]lyrics",
        encoding="utf-8",
    )

    rows, _, _ = app.scan_library(tmp_path)

    assert len(rows) == 2
    assert sum(row.status == "가사 있음" for row in rows) == 1
    assert sum(row.status == "가사 없음" for row in rows) == 1


def test_lrc_prefers_exact_stem_then_same_directory(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    (left / "Artist A - Hello.flac").write_bytes(b"a")
    (right / "Artist B - Hello.flac").write_bytes(b"b")

    exact = left / "Artist A - Hello.lrc"
    exact.write_text("[ti:Hello]\n[00:01.00]left", encoding="utf-8")
    other = right / "different.lrc"
    other.write_text("[ti:Hello]\n[00:01.00]right", encoding="utf-8")

    rows, _, _ = app.scan_library(tmp_path)
    by_artist = {row.flac.artist: row for row in rows}

    assert by_artist["Artist A"].lrc.path == exact
    assert by_artist["Artist B"].lrc.path == other


def test_dedup_does_not_reorder_artist_credit():
    one = app.FlacItem(
        Path("one.flac"),
        "Song",
        "A, B",
        ("A", "B"),
        {},
        (1, 1, 16, 44100, 100),
    )
    two = app.FlacItem(
        Path("two.flac"),
        "Song",
        "B, A",
        ("B", "A"),
        {},
        (1, 1, 16, 44100, 90),
    )

    kept, removed = app.deduplicate_flacs([one, two])

    assert removed == 0
    assert len(kept) == 2

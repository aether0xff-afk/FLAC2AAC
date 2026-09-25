from pathlib import Path
import app


def test_clean_lrc():
    assert app.clean_lrc('[ar:A]\n[00:01.00]hello') == 'hello'


def test_flat_output_names():
    row = app.Row(app.FlacItem(Path('x.flac'), 'Song', 'Artist'), None, 'x')
    app.assign_flat_output_names([row])
    assert row.output_name == 'Artist - Song.m4a'


def test_multiple_artists_use_commas():
    assert app.join_artist_values(["Artist A", "Artist B"]) == "Artist A, Artist B"
    assert app.join_artist_values(["Artist A; Artist B"]) == "Artist A, Artist B"

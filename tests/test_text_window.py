import pytest

from django_ergo.conversation.text_window import read_window


def test_short_text_is_unchanged():
    assert read_window("é\nworld", "x") == "é\nworld"


def test_range_and_continuation():
    result = read_window(
        "one\ntwo\nthree\nfour",
        "x",
        start_line=2,
        max_chars=8,
        sha256="abcdef1234567890",
        version=3,
    )
    assert "x lines 2-2 of 4 · sha256 abcdef123456 · version 3" in result
    assert "2: two" in result and "start_line=3" in result
    assert read_window("a\nb", "x", start_line=3) == "x has 2 lines"


def test_long_line_and_invalid_range():
    assert "[line 1 cut at" in read_window("abcdefgh", "x", max_chars=5)
    for args in ({"start_line": 0}, {"start_line": 3, "end_line": 2}):
        with pytest.raises(ValueError, match="line"):
            read_window("a", "x", **args)

import pytest

from app.core.security import MAX_DISPLAY_FILENAME_LENGTH, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("survey.kml", "survey.kml"),
        ("../../evil.py", "evil.py"),
        ("..\\..\\windows\\evil.exe", "evil.exe"),
        ("/etc/passwd", "passwd"),
        ("name\x00with\nnulls.kml", "name_with_nulls.kml"),
        ("a<b>c.kml", "a_b_c.kml"),
        ("<script>alert(1)</script>.kml", "script_.kml"),
        ("..", "upload"),
        ("", "upload"),
        ("  .hidden  ", "hidden"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_limits_length() -> None:
    assert len(sanitize_filename("a" * 1000 + ".kml")) == MAX_DISPLAY_FILENAME_LENGTH

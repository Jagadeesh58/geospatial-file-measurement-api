import re
import unicodedata

MAX_DISPLAY_FILENAME_LENGTH = 255
FALLBACK_FILENAME = "upload"
_DISALLOWED_CHARACTERS = re.compile(r"[^\w.\- ]+")


def sanitize_filename(filename: str) -> str:
    """Reduce a client-supplied name to a harmless display string.

    The result is stored and echoed back for display only. Files are always saved under
    generated names, so this value is never used to build a filesystem path.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKC", name)
    name = _DISALLOWED_CHARACTERS.sub("_", name).strip(" .")
    return name[:MAX_DISPLAY_FILENAME_LENGTH] or FALLBACK_FILENAME

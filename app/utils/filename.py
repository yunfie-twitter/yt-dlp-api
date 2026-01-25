import re
import unicodedata


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitize filename for cross-platform compatibility"""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\\/:*?"<>|]', '_', name)

    windows_reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }
    if name.upper() in windows_reserved:
        name = f"_{name}"

    return name[:max_length].strip()

"""Proof image normalisation at upload (agent layer proof-images.md, "Proposal: normalise at
upload", and its integrity flags).

Reads what matters from the metadata first, then decodes, rotates upright, strips every bit of
metadata by re-encoding, resizes and keeps the result small enough for every Claude platform.
The agent only ever sees the normalised copy; the original is kept encrypted as evidence.
"""

import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_PIXELS = 40_000_000  # decompression-bomb guard, before decoding
MAX_LONG_EDGE = 2576
MIN_SHORT_EDGE = 200
MAX_STORED_BYTES = int(3.75 * 1024 * 1024)
JPEG_QUALITIES = (85, 75, 65, 55)
OLD_CAPTURE = timedelta(days=30)
SCREEN_ASPECT = 1.9  # phone screens are ~19.5:9; camera photos are 4:3 or 3:2
NEAR_DUPLICATE_BITS = 8  # dHash distance (of 64) that counts as the same picture

# EXIF tags (TIFF numbering).
MAKE, MODEL, SOFTWARE, DATETIME = 0x010F, 0x0110, 0x0131, 0x0132
EXIF_IFD, DATETIME_ORIGINAL = 0x8769, 0x9003
EDITORS = re.compile(
    r"photoshop|lightroom|gimp|snapseed|picsart|canva|pixelmator|affinity|facetune|meitu|"
    r"photoroom|adobe",
    re.IGNORECASE,
)


class ImageRejectedError(ValueError):
    """Not an image the platform accepts as proof."""


@dataclass(frozen=True)
class NormalisedImage:
    data: bytes
    media_type: str
    dhash: int  # 64-bit perceptual hash of the stored image, as a signed integer
    flags: list[str] = field(default_factory=list)


def _metadata_flags(image: Image.Image, fmt: str, now: datetime) -> list[str]:
    exif = image.getexif()
    make = str(exif.get(MAKE, "")).strip()
    model = str(exif.get(MODEL, "")).strip()
    software = str(exif.get(SOFTWARE, "")).strip()
    taken = exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL) or exif.get(DATETIME)

    flags = []
    if not make and not model:
        flags.append("no_camera_metadata")
        width, height = image.size
        if fmt == "PNG" or max(width, height) / max(1, min(width, height)) >= SCREEN_ASPECT:
            flags.append("screenshot")
    if software and EDITORS.search(software):
        flags.append("edited")
    if taken:
        # EXIF times carry no zone; read as UTC, since a day either way doesn't change a
        # 30-day rule.
        try:
            captured = datetime.strptime(str(taken).strip(), "%Y:%m:%d %H:%M:%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            captured = None
        if captured is not None and now - captured > OLD_CAPTURE:
            flags.append("old_capture")
    return flags


def dhash(image: Image.Image) -> int:
    """Difference hash: survives resizing and re-compression, unlike SHA-256."""
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = cast(list[int], list(small.get_flattened_data()))  # one band ("L"): plain ints
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits - (1 << 64) if bits >= 1 << 63 else bits  # fits a signed BIGINT


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def _encode(image: Image.Image, as_png: bool) -> tuple[bytes, str]:
    if as_png:
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        if out.tell() <= MAX_STORED_BYTES:
            return out.getvalue(), "image/png"
    rgb = image.convert("RGB")
    while True:
        for quality in JPEG_QUALITIES:
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=quality, optimize=True)
            if out.tell() <= MAX_STORED_BYTES:
                return out.getvalue(), "image/jpeg"
        # Still too big at the lowest quality: shrink and try again.
        rgb = rgb.resize((int(rgb.width * 0.8), int(rgb.height * 0.8)), Image.Resampling.LANCZOS)


def normalise(raw: bytes, now: datetime) -> NormalisedImage:
    try:
        image = Image.open(io.BytesIO(raw))
        fmt = image.format or ""
        if fmt not in ALLOWED_FORMATS:
            raise ImageRejectedError("only JPEG, PNG or WebP")
        if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
            raise ImageRejectedError("animated images are not accepted")
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise ImageRejectedError("image is too large")
        flags = _metadata_flags(image, fmt, now)
        image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageRejectedError("not a readable image") from exc

    upright = ImageOps.exif_transpose(image)  # before the orientation tag is stripped
    if min(upright.size) < MIN_SHORT_EDGE:
        raise ImageRejectedError("image is too small to read")
    upright.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)  # never upscales

    keep_png = fmt == "PNG" and ("screenshot" in flags or upright.mode in ("RGBA", "LA", "P"))
    data, media_type = _encode(upright, as_png=keep_png)
    stored = Image.open(io.BytesIO(data))
    return NormalisedImage(data=data, media_type=media_type, dhash=dhash(stored), flags=flags)

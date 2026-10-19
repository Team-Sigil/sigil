import io
import random
from datetime import UTC, datetime

import pytest
from PIL import Image, ImageDraw

from gateway.proof_images import (
    MAX_LONG_EDGE,
    MAX_STORED_BYTES,
    NEAR_DUPLICATE_BITS,
    ImageRejectedError,
    hamming,
    normalise,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def picture(width: int, height: int, seed: int = 1) -> Image.Image:
    """A receipt-like picture with structure, so perceptual hashes mean something."""
    rng = random.Random(seed)
    image = Image.new("RGB", (width, height), (240, 236, 228))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = rng.randrange(width), rng.randrange(height)
        shade = rng.randrange(0, 160)
        draw.rectangle([x, y, x + width // 6, y + height // 30], fill=(shade, shade, shade))
    return image


def encode(image: Image.Image, *, fmt: str = "JPEG", **exif_tags: object) -> bytes:
    exif = Image.Exif()
    gps = exif_tags.pop("gps", None)
    ifd_exif = exif_tags.pop("taken", None)
    for tag, value in exif_tags.items():
        exif[int(tag.removeprefix("t"), 16)] = value
    if gps:
        exif.get_ifd(0x8825)[2] = gps  # GPSLatitude
    if ifd_exif:
        exif.get_ifd(0x8769)[0x9003] = ifd_exif
    out = io.BytesIO()
    image.save(out, format=fmt, exif=exif.tobytes())
    return out.getvalue()


CAMERA = {"t010F": "Apple", "t0110": "iPhone 16"}


def test_camera_photo_is_resized_and_stripped_of_metadata() -> None:
    raw = encode(picture(3000, 4000), **CAMERA, gps=(13.0, 45.0, 0.0))
    result = normalise(raw, NOW)
    stored = Image.open(io.BytesIO(result.data))
    assert result.media_type == "image/jpeg" and max(stored.size) == MAX_LONG_EDGE
    assert dict(stored.getexif()) == {}  # no make, model or GPS left
    assert result.flags == []


def test_orientation_is_applied_before_stripping() -> None:
    raw = encode(picture(1600, 1200), **CAMERA, t0112=6)  # "rotate 90° clockwise"
    stored = Image.open(io.BytesIO(normalise(raw, NOW).data))
    assert stored.size == (1200, 1600)


def test_screenshot_is_flagged_and_kept_as_png() -> None:
    raw = encode(picture(1170, 2532), fmt="PNG")
    result = normalise(raw, NOW)
    assert result.media_type == "image/png"
    assert result.flags == ["no_camera_metadata", "screenshot"]


@pytest.mark.parametrize(
    ("tags", "flag"),
    [
        ({"t0131": "Adobe Photoshop 26.0"}, "edited"),
        ({"taken": "2026:01:02 09:00:00"}, "old_capture"),
    ],
)
def test_metadata_flags(tags: dict[str, str], flag: str) -> None:
    raw = encode(picture(1200, 1600), **CAMERA, **tags)
    assert flag in normalise(raw, NOW).flags


def test_recent_capture_is_not_old() -> None:
    raw = encode(picture(1200, 1600), **CAMERA, taken="2026:10:17 09:00:00")
    assert "old_capture" not in normalise(raw, NOW).flags


def test_stays_under_the_size_limit() -> None:
    rng = random.Random(7)
    noise = Image.frombytes(
        "RGB", (2576, 2576), bytes(rng.randrange(256) for _ in range(2576 * 2576 * 3))
    )
    assert len(normalise(encode(noise, fmt="PNG"), NOW).data) <= MAX_STORED_BYTES


def test_same_receipt_photographed_again_is_near_duplicate() -> None:
    first = normalise(encode(picture(2000, 2600, seed=3), **CAMERA), NOW)
    again = picture(2000, 2600, seed=3).resize((1500, 1950))
    second = normalise(encode(again, **CAMERA), NOW)
    other = normalise(encode(picture(2000, 2600, seed=9), **CAMERA), NOW)
    assert hamming(first.dhash, second.dhash) <= NEAR_DUPLICATE_BITS
    assert hamming(first.dhash, other.dhash) > NEAR_DUPLICATE_BITS


@pytest.mark.parametrize(
    "raw",
    [
        b"not an image",
        b"\\xff\\xd8\\xff truncated jpeg",
    ],
)
def test_garbage_is_rejected(raw: bytes) -> None:
    with pytest.raises(ImageRejectedError):
        normalise(raw, NOW)


def test_gif_and_animation_are_rejected() -> None:
    with pytest.raises(ImageRejectedError, match="JPEG, PNG or WebP"):
        normalise(encode(picture(400, 400), fmt="GIF"), NOW)
    frames = [picture(400, 400, seed=s) for s in (1, 2)]
    out = io.BytesIO()
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:])
    with pytest.raises(ImageRejectedError, match="animated"):
        normalise(out.getvalue(), NOW)


def test_tiny_image_is_rejected() -> None:
    with pytest.raises(ImageRejectedError, match="too small"):
        normalise(encode(picture(150, 150)), NOW)

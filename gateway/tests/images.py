"""Real, decodable test images (the gateway decodes and re-encodes every proof image)."""

import io
import random

from PIL import Image, ImageDraw


def picture(seed: int = 1, size: tuple[int, int] = (600, 800)) -> Image.Image:
    rng = random.Random(seed)
    image = Image.new("RGB", size, (240, 236, 228))
    draw = ImageDraw.Draw(image)
    for _ in range(30):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        shade = rng.randrange(0, 160)
        draw.rectangle([x, y, x + size[0] // 5, y + size[1] // 25], fill=(shade, shade, shade))
    return image


def jpeg(seed: int = 1, size: tuple[int, int] = (600, 800)) -> bytes:
    """Like an iPhone photo: JPEG with camera make and model in its EXIF."""
    return jpeg_of(picture(seed, size))


def jpeg_of(image: Image.Image) -> bytes:
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "Apple", "iPhone 16"
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90, exif=exif.tobytes())
    return out.getvalue()


def png(seed: int = 1, size: tuple[int, int] = (600, 800)) -> bytes:
    out = io.BytesIO()
    picture(seed, size).save(out, format="PNG")
    return out.getvalue()

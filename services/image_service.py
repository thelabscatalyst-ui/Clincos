"""
image_service.py — every image a doctor uploads is stored as a JPEG.

Why: iPhones save photos as HEIC by default, and Chrome, Firefox and Edge
cannot display HEIC. A doctor photographing a lab report on an iPhone uploaded
a file nobody could preview — clicking it opened a blank tab, and "Download"
saved something Windows usually cannot open. The first real upload to
production was exactly that: an iPhone photo, stored perfectly, viewable by no
one.

The rule:

  * Anything that is an image — HEIC/HEIF, PNG, WebP, GIF, TIFF, BMP, and JPEG
    itself — is decoded and re-encoded as JPEG.
  * Everything else (PDF, Word, plain text…) is stored exactly as uploaded.
    A multi-page PDF lab report flattened to one JPEG would lose pages and its
    searchable text.
  * An image that cannot be decoded is rejected, not stored as-is. "Images
    are JPEG" is a promise; a file the browser cannot show would break it.

Re-encoding JPEGs too is deliberate. It applies the phone's rotation flag
(portrait photos are otherwise stored sideways), strips EXIF — including the
GPS position every phone camera embeds — and means whatever is stored is
something we produced, not attacker-controlled bytes behind an image/*
content type. It also closes SVG: Pillow cannot rasterise SVG, so it is
rejected rather than served inline, where its script would run.
"""
import io
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import PurePath
from typing import Optional

logger = logging.getLogger(__name__)

JPEG_QUALITY = 90        # visually lossless for photographed documents
MAX_EDGE = 4096          # px on the long side; far beyond what a report needs

# Extensions that mean "image" even when mimetypes does not know them.
_IMAGE_EXTS = {".heic", ".heif", ".hif", ".avif", ".webp", ".jfif", ".tif", ".tiff"}

_heif_ready = False


def _register_heif() -> None:
    """Teach Pillow to read HEIC/HEIF. Idempotent, and tolerant of the library
    being absent — then HEIC uploads are rejected as undecodable rather than
    crashing the request."""
    global _heif_ready
    if _heif_ready:
        return
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        _heif_ready = True
    except Exception as exc:                      # pragma: no cover - env dependent
        logger.error("HEIC support unavailable (%s) — iPhone photos will be rejected", exc)


@dataclass
class Prepared:
    name: str          # what the doctor sees, e.g. "Sample Photo.jpg"
    data: bytes
    mime: str
    converted: bool    # True if this was an image re-encoded to JPEG


def is_image(filename: str) -> bool:
    """Decide by name. Sniffing every PDF through Pillow would be wasted work,
    and a mislabelled file is stored as what it claims to be — harmless, since
    only image/* is ever previewed inline."""
    ext = PurePath(filename or "").suffix.lower()
    if ext in _IMAGE_EXTS:
        return True
    mime, _ = mimetypes.guess_type(filename or "")
    return bool(mime and mime.startswith("image/"))


def jpeg_name(filename: str) -> str:
    stem = PurePath(filename or "").stem or "image"
    return f"{stem}.jpg"


def to_jpeg(data: bytes) -> Optional[bytes]:
    """Decode any image Pillow (+ pillow-heif) understands and return JPEG
    bytes, or None if it is not a decodable image. Never raises."""
    _register_heif()
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)                      # first frame of a GIF / HEIC burst
            im = ImageOps.exif_transpose(im)  # apply the phone's rotation flag

            if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
                # 16-bit / float greyscale — common in scans. A plain convert
                # clips everything above 255 to white; scale to 8-bit instead.
                im = im.convert("I")
                lo, hi = im.getextrema()
                span = (hi - lo) or 1
                im = im.point(lambda v: (v - lo) * 255.0 / span).convert("L")

            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                # JPEG has no alpha. Flatten onto white, which is what a
                # transparent PNG looks like on a page anyway.
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            elif im.mode != "RGB" and im.mode != "L":
                im = im.convert("RGB")      # CMYK, P, YCbCr, …

            if max(im.size) > MAX_EDGE:
                im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

            out = io.BytesIO()
            # No exif= argument: metadata (GPS included) is deliberately dropped.
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return out.getvalue()
    except Exception as exc:
        logger.warning("image could not be converted to JPEG (%s: %s)",
                       type(exc).__name__, exc)
        return None


def prepare_upload(filename: str, data: bytes) -> Optional[Prepared]:
    """What to actually store for one uploaded file.

    Images come back as JPEG with a .jpg name; anything else comes back
    untouched. Returns None only for an image that cannot be decoded — the
    caller should refuse it rather than store something unviewable.
    """
    if not is_image(filename):
        mime, _ = mimetypes.guess_type(filename or "")
        return Prepared(name=filename, data=data,
                        mime=mime or "application/octet-stream", converted=False)

    jpeg = to_jpeg(data)
    if jpeg is None:
        return None
    return Prepared(name=jpeg_name(filename), data=jpeg, mime="image/jpeg", converted=True)

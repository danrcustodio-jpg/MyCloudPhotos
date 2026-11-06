from __future__ import annotations

import hashlib
import io
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ExifTags
import imagehash

from app.config import IMAGE_EXTENSIONS, RAW_EXTENSIONS, THUMB_SIZE, VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

# Enable HEIC/HEIF (iPhone photos) for Pillow
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

# Quiet Pillow for large images / truncated files
Image.MAX_IMAGE_PIXELS = 50_000_000

_EXIF_DATE_TAGS = {"DateTimeOriginal", "DateTimeDigitized", "DateTime"}


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_media(path: Path) -> bool:
    return is_image(path) or is_video(path)


def decode_raw_to_image(path: Path) -> tuple[Image.Image, datetime | None]:
    """
    Decode a camera RAW file (or a JPEG misnamed as RAW) to an RGB Pillow image.
    Returns (image, capture_datetime_or_None).
    """
    # Many "NEF" files on this library are actually JPEGs with a .NEF extension
    # (Google Photos / Drive export stubs). Detect that before calling LibRaw.
    try:
        with path.open("rb") as f:
            magic = f.read(3)
    except OSError:
        magic = b""

    if magic.startswith(b"\xff\xd8\xff"):
        with open_image(path) as img:
            # Copy so file handle can close; keep RGB
            out = img.convert("RGB") if img.mode != "RGB" else img.copy()
            capture_dt = _capture_dt_from_pil(img)
        return out, capture_dt

    import rawpy

    try:
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=False,
                output_bps=8,
                bright=1.0,
            )
            capture_dt: datetime | None = None
            try:
                ts = getattr(raw, "timestamp", None)
                if ts:
                    capture_dt = datetime.fromtimestamp(float(ts))
            except (TypeError, ValueError, OSError, OverflowError):
                capture_dt = None
            return Image.fromarray(rgb), capture_dt
    except Exception:
        # Fall back: embedded JPEG preview inside a real RAW
        try:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with open_image_bytes(thumb.data) as img:
                    out = img.convert("RGB") if img.mode != "RGB" else img.copy()
                    capture_dt = _capture_dt_from_pil(img)
                return out, capture_dt
        except Exception:
            pass
        raise


def _capture_dt_from_pil(img: Image.Image) -> datetime | None:
    exif = _exif_dict(img)
    for key in _EXIF_DATE_TAGS:
        value = exif.get(key)
        if not value or not isinstance(value, str):
            continue
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
            try:
                return datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
    return None


def file_sha256(
    path: Path | None = None,
    chunk_size: int = 1024 * 1024,
    *,
    data: bytes | None = None,
) -> str:
    """Hash a file path or in-memory bytes (import preview / analyze_image)."""
    h = hashlib.sha256()
    if data is not None:
        h.update(data)
        return h.hexdigest()
    if path is None:
        raise ValueError("file_sha256 requires path or data")
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _exif_dict(img: Image.Image) -> dict[str, Any]:
    raw = img.getexif()
    if not raw:
        return {}
    tagged: dict[str, Any] = {}
    for tag_id, value in raw.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        tagged[name] = value
    # Also walk IFD for DateTimeOriginal when present
    try:
        for ifd_id in raw.get_ifd(0x8769) or {}:
            name = ExifTags.TAGS.get(ifd_id, str(ifd_id))
            tagged[name] = raw.get_ifd(0x8769)[ifd_id]
    except Exception:
        pass
    # GPS IFD
    try:
        gps_ifd = raw.get_ifd(0x8825)
        if gps_ifd:
            tagged["GPSInfo"] = gps_ifd
    except Exception:
        pass
    return tagged


def _ratio_to_float(value: Any) -> float | None:
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            den = float(value.denominator)
            return float(value.numerator) / den if den else None
        if isinstance(value, (tuple, list)) and len(value) == 2:
            den = float(value[1])
            return float(value[0]) / den if den else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_to_degrees(values: Any) -> float | None:
    if not values or len(values) < 3:
        return None
    parts = [_ratio_to_float(v) for v in values[:3]]
    if any(p is None for p in parts):
        return None
    d, m, s = parts  # type: ignore[misc]
    return d + m / 60.0 + s / 3600.0


def extract_gps(exif: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (latitude, longitude) from EXIF GPSInfo, or (None, None)."""
    gps = exif.get("GPSInfo")
    if not gps:
        return None, None

    # Pillow may return numeric IFD keys or already-tagged names
    def _g(key_num: int, key_name: str) -> Any:
        if isinstance(gps, dict):
            if key_num in gps:
                return gps[key_num]
            if key_name in gps:
                return gps[key_name]
            # GPSTAGS reverse lookup
            for k, v in gps.items():
                name = ExifTags.GPSTAGS.get(k, k) if isinstance(k, int) else k
                if name == key_name:
                    return v
        return None

    lat_ref = _g(1, "GPSLatitudeRef")
    lat_vals = _g(2, "GPSLatitude")
    lon_ref = _g(3, "GPSLongitudeRef")
    lon_vals = _g(4, "GPSLongitude")

    lat = _gps_to_degrees(lat_vals)
    lon = _gps_to_degrees(lon_vals)
    if lat is None or lon is None:
        return None, None

    if isinstance(lat_ref, str) and lat_ref.upper() == "S":
        lat = -lat
    if isinstance(lon_ref, str) and lon_ref.upper() == "W":
        lon = -lon
    return lat, lon


def parse_exif_date(img: Image.Image) -> str | None:
    exif = _exif_dict(img)
    for key in _EXIF_DATE_TAGS:
        value = exif.get(key)
        if not value or not isinstance(value, str):
            continue
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
            try:
                return datetime.strptime(value.strip(), fmt).isoformat(sep=" ")
            except ValueError:
                continue
    return None


def open_image_bytes(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def open_image(path: Path) -> Image.Image:
    data = path.read_bytes()
    return open_image_bytes(data)


def compute_phash(img: Image.Image) -> str:
    rgb = img.convert("RGB")
    return str(imagehash.phash(rgb))


def make_thumbnail(img: Image.Image, dest: Path, size: int = THUMB_SIZE) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    thumb = img.copy()
    thumb.thumbnail((size, size), Image.Resampling.BICUBIC)
    if thumb.mode not in ("RGB", "L"):
        thumb = thumb.convert("RGB")
    thumb.save(dest, format="JPEG", quality=80)


def make_video_placeholder(dest: Path, size: int = THUMB_SIZE) -> None:
    """Solid-color JPEG labeled VIDEO (fallback when frame extract fails)."""
    from PIL import ImageDraw, ImageFont

    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (size, size), (30, 42, 56))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, size // 2 - 28, size, size // 2 + 28], fill=(45, 90, 110))
    label = "VIDEO"
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 2), label, fill=(232, 238, 245), font=font)
    cx, cy = size // 2, size // 2 + 48
    draw.polygon([(cx - 10, cy - 12), (cx - 10, cy + 12), (cx + 14, cy)], fill=(61, 154, 122))
    img.save(dest, format="JPEG", quality=80)


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which("ffmpeg")


def is_video_placeholder_thumb(path: Path, size: int = THUMB_SIZE) -> bool:
    """True when thumb looks like the solid VIDEO placeholder (not a real frame)."""
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return True
        # Real frames are usually larger; placeholders are ~10–20 KB.
        if path.stat().st_size > 28_000:
            return False
        with Image.open(path) as im:
            return im.size == (size, size)
    except Exception:
        return True


def extract_video_frame_thumb(
    video_path: Path, dest: Path, size: int = THUMB_SIZE
) -> bool:
    """
    Extract one frame near 1s (fallback 0s) via ffmpeg and save a JPEG thumb.
    Returns True on success.
    """
    ff = _ffmpeg_exe()
    if not ff or not video_path.exists():
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".frame.tmp.jpg")
    for ss in ("1", "0"):
        try:
            if raw.exists():
                raw.unlink(missing_ok=True)
            cmd = [
                ff,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                ss,
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                "-y",
                str(raw),
            ]
            subprocess.run(
                cmd,
                check=True,
                timeout=90,
                capture_output=True,
            )
            if not raw.exists() or raw.stat().st_size <= 0:
                continue
            with Image.open(raw) as im:
                im.load()
                make_thumbnail(im, dest, size=size)
            raw.unlink(missing_ok=True)
            return dest.exists() and dest.stat().st_size > 0
        except Exception as e:
            logger.debug("ffmpeg thumb ss=%s failed for %s: %s", ss, video_path.name, e)
            raw.unlink(missing_ok=True)
            continue
    return False


def make_video_thumbnail(
    dest: Path,
    size: int = THUMB_SIZE,
    *,
    video_path: Path | None = None,
) -> None:
    """
    Write a video thumb JPEG. Prefer a real frame from video_path when given;
    otherwise (or on failure) write the VIDEO placeholder.
    """
    if video_path is not None:
        try:
            if extract_video_frame_thumb(video_path, dest, size=size):
                return
        except Exception as e:
            logger.debug("video frame thumb failed for %s: %s", video_path, e)
    make_video_placeholder(dest, size=size)


def quick_content_hash(path: Path, size: int, mtime: float) -> str:
    """Fast stable hash from size+mtime (large videos)."""
    payload = f"{path.name}|{size}|{mtime:.6f}".encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()


def analyze_video(path: Path, thumb_dest: Path) -> dict[str, Any]:
    """
    Index a video; build a frame thumb when ffmpeg is available.
    Full SHA-256 for files <= 50 MB; size+mtime shortcut otherwise.
    """
    stat = path.stat()
    if stat.st_size <= 50 * 1024 * 1024:
        content_hash = file_sha256(path)
    else:
        content_hash = quick_content_hash(path, stat.st_size, stat.st_mtime)
    make_video_thumbnail(thumb_dest, video_path=path)
    return {
        "width": None,
        "height": None,
        "exif_date": None,
        "latitude": None,
        "longitude": None,
        "content_hash": content_hash,
        "phash": None,
        "thumb_path": str(thumb_dest),
        "media_type": "video",
        "duration": None,
    }


def analyze_image(path: Path, thumb_dest: Path) -> dict[str, Any]:
    """Return metadata dict for an image file. Reads file bytes once. Raises on failure."""
    data = path.read_bytes()
    content_hash = file_sha256(data=data)
    with open_image_bytes(data) as img:
        width, height = img.size
        exif = _exif_dict(img)
        exif_date = None
        for key in _EXIF_DATE_TAGS:
            value = exif.get(key)
            if not value or not isinstance(value, str):
                continue
            for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
                try:
                    exif_date = datetime.strptime(value.strip(), fmt).isoformat(sep=" ")
                    break
                except ValueError:
                    continue
            if exif_date:
                break
        latitude, longitude = extract_gps(exif)
        phash = compute_phash(img)
        make_thumbnail(img, thumb_dest)
    return {
        "width": width,
        "height": height,
        "exif_date": exif_date,
        "latitude": latitude,
        "longitude": longitude,
        "content_hash": content_hash,
        "phash": phash,
        "thumb_path": str(thumb_dest),
        "media_type": "image",
        "duration": None,
    }


def hamming_distance(hash_a: str, hash_b: str) -> int:
    try:
        return imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b)
    except Exception:
        return 64

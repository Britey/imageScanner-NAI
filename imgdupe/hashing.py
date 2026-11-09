from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image, ImageOps


EDGE_CROP_FRACTIONS = (0.25, 0.33, 0.40, 0.50, 0.60, 0.67, 0.75)
CENTER_CROP_FRACTIONS = (0.95, 0.90, 0.80, 0.70, 0.60, 0.50)
TRYHARD_TILE_LAYOUTS = (
    (1, 1),
    (2, 1),
    (1, 2),
    (3, 1),
    (1, 3),
    (2, 2),
    (3, 2),
    (2, 3),
    (4, 2),
    (2, 4),
)


class ImageTooSmallError(ValueError):
    pass


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.digest()


def load_normalized(path: Path, *, min_width: int = 32, min_height: int = 32) -> Image.Image:
    with Image.open(path) as opened:
        image_format = opened.format
        img = ImageOps.exif_transpose(opened)
        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, rgba).convert("RGB")
        else:
            img = img.convert("RGB")

    if img.width < min_width or img.height < min_height:
        raise ImageTooSmallError(f"image is too small: {img.width}x{img.height}")
    img.info["source_format"] = image_format
    return img


def imagehash_to_bytes(hash_value: imagehash.ImageHash) -> bytes:
    bits = hash_value.hash.flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    byte_len = (len(bits) + 7) // 8
    return value.to_bytes(byte_len, "big")


def grid_hashes(img: Image.Image) -> list[bytes]:
    width, height = img.size
    results: list[bytes] = []
    for gy in range(3):
        for gx in range(3):
            left = width * gx // 3
            upper = height * gy // 3
            right = width * (gx + 1) // 3
            lower = height * (gy + 1) // 3
            crop = img.crop((left, upper, right, lower))
            results.append(imagehash_to_bytes(imagehash.phash(crop, hash_size=8)))
    return results


def crop_region_hashes(img: Image.Image) -> dict[str, bytes]:
    width, height = img.size
    boxes = crop_region_boxes(width, height)
    results = {}
    for name, box in boxes.items():
        crop = img.crop(box)
        if crop.width >= 32 and crop.height >= 32:
            results[name] = imagehash_to_bytes(imagehash.phash(crop, hash_size=16))
    return results


def tryhard_query_hashes(img: Image.Image) -> dict[str, bytes]:
    results = {
        f"crop:{name}": value
        for name, value in crop_region_hashes(img).items()
    }
    for name, value in tile_hashes(img).items():
        results[f"tile:{name}"] = value
    return results


def tile_hashes(img: Image.Image) -> dict[str, bytes]:
    width, height = img.size
    results = {}
    for columns, rows in TRYHARD_TILE_LAYOUTS:
        for row in range(rows):
            for column in range(columns):
                left = width * column // columns
                upper = height * row // rows
                right = width * (column + 1) // columns
                lower = height * (row + 1) // rows
                crop = img.crop((left, upper, right, lower))
                if crop.width >= 32 and crop.height >= 32:
                    name = f"{columns}x{rows}_{column}_{row}"
                    results[name] = imagehash_to_bytes(imagehash.phash(crop, hash_size=16))
    return results


def crop_region_boxes(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for fraction in EDGE_CROP_FRACTIONS:
        label = _fraction_label(fraction)
        crop_width = max(1, int(width * fraction))
        crop_height = max(1, int(height * fraction))
        boxes[f"top_{label}"] = (0, 0, width, crop_height)
        boxes[f"bottom_{label}"] = (0, height - crop_height, width, height)
        boxes[f"left_{label}"] = (0, 0, crop_width, height)
        boxes[f"right_{label}"] = (width - crop_width, 0, width, height)
    for fraction in CENTER_CROP_FRACTIONS:
        boxes[f"center_{_fraction_label(fraction)}"] = _center_box(width, height, fraction)
    for y_name, upper, lower in (
        ("top", 0, height // 2),
        ("bottom", height // 2, height),
    ):
        for x_name, left, right in (
            ("left", 0, width // 2),
            ("right", width // 2, width),
        ):
            boxes[f"{y_name}_{x_name}_quarter"] = (left, upper, right, lower)
    return boxes


def _fraction_label(fraction: float) -> str:
    return str(int(round(fraction * 100)))


def _center_box(width: int, height: int, fraction: float) -> tuple[int, int, int, int]:
    crop_width = max(1, int(width * fraction))
    crop_height = max(1, int(height * fraction))
    left = (width - crop_width) // 2
    upper = (height - crop_height) // 2
    return left, upper, left + crop_width, upper + crop_height


def compute_image_hashes(
    path: Path,
    *,
    min_width: int = 32,
    min_height: int = 32,
    include_crop_regions: bool = False,
    include_query_tiles: bool = False,
    precomputed_sha256: bytes | None = None,
) -> tuple[dict[str, bytes], dict[str, int | str]]:
    img = load_normalized(path, min_width=min_width, min_height=min_height)
    hashes = {
        "sha256": precomputed_sha256 if precomputed_sha256 is not None else sha256_file(path),
        "dhash256": imagehash_to_bytes(imagehash.dhash(img, hash_size=16)),
        "phash256": imagehash_to_bytes(imagehash.phash(img, hash_size=16)),
        "whash256": imagehash_to_bytes(imagehash.whash(img, hash_size=16)),
    }
    for index, value in enumerate(grid_hashes(img)):
        hashes[f"grid{index}"] = value
    if include_crop_regions or include_query_tiles:
        for name, value in crop_region_hashes(img).items():
            hashes[f"crop:{name}"] = value
    if include_query_tiles:
        for name, value in tile_hashes(img).items():
            hashes[f"tile:{name}"] = value
    metadata = {
        "width": img.width,
        "height": img.height,
        "format": str(img.info.get("source_format") or ""),
    }
    return hashes, metadata


def hamming_bytes(a: bytes, b: bytes) -> int:
    if len(a) != len(b):
        raise ValueError(f"cannot compare hashes with lengths {len(a)} and {len(b)}")
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()

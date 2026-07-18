from dataclasses import dataclass


@dataclass(frozen=True)
class ScanConfig:
    min_width: int = 32
    min_height: int = 32
    batch_size: int = 500
    # Keep enough parallelism to overlap decoding and perceptual hashing without
    # flooding a mechanical disk with the larger queues used by older builds.
    workers: int = 4
    crop_index: bool = False
    prompt_index: bool = True
    stealth_prompt: bool = False
    reindex_prompts: bool = False
    delete_missing: bool = False
    whole_band_size: int = 2
    grid_band_size: int = 2


@dataclass(frozen=True)
class Thresholds:
    phash_strong: int = 16
    phash_probable: int = 32
    phash_review: int = 40
    whash_strong: int = 16
    whash_probable: int = 32
    whash_review: int = 40
    dhash_strong: int = 16
    dhash_probable: int = 32
    dhash_review: int = 40
    grid_cell: int = 6
    grid_strong: int = 7
    grid_probable: int = 5
    grid_review: int = 4
    score_strong: float = 85.0
    score_probable: float = 70.0
    score_review: float = 55.0


IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

"""Deterministic asymmetric images for perceptual-search behavior tests."""
from __future__ import annotations

import numpy as np
from PIL import Image


def textured(seed: int, size: tuple[int, int] = (256, 192)) -> Image.Image:
    rng = np.random.default_rng(seed)
    coarse = Image.fromarray(rng.integers(0, 256, (12, 16, 3), dtype=np.uint8)).resize(size, Image.Resampling.BICUBIC)
    fine = Image.fromarray(rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)).resize(size, Image.Resampling.BILINEAR)
    return Image.blend(coarse, fine, 0.3)

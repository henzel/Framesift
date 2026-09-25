from __future__ import annotations

import numpy as np
from make_dataset import picture
from PIL import ImageFilter

from framesift.engine.analysis import brightness_of, hamming, phash_from_gray, sharpness_of


def _hash(im) -> int:
    g = im.convert("L").resize((32, 32))
    return phash_from_gray(np.asarray(g, dtype=np.float64))


def test_phash_distances() -> None:
    a = picture(1)
    b = a.copy()
    b.putpixel((3, 3), (255, 255, 255))
    c = picture(2)
    assert hamming(_hash(a), _hash(a)) == 0
    assert hamming(_hash(a), _hash(b)) <= 4
    assert hamming(_hash(a), _hash(a.resize((160, 120)))) <= 6
    assert hamming(_hash(a), _hash(c)) > 12


def test_sharpness_and_darkness() -> None:
    sharp = picture(3)
    blurry = sharp.filter(ImageFilter.GaussianBlur(10))
    s1 = sharpness_of(np.asarray(sharp.convert("L"), dtype=np.float32))
    s2 = sharpness_of(np.asarray(blurry.convert("L"), dtype=np.float32))
    assert s1 > 50 and s2 < 12 and s2 < s1
    dark = picture(4, dark=True)
    mean, p99 = brightness_of(np.asarray(dark.convert("L"), dtype=np.float32))
    assert mean < 0.03 and p99 < 0.12
    mean2, _ = brightness_of(np.asarray(sharp.convert("L"), dtype=np.float32))
    assert mean2 > 0.1

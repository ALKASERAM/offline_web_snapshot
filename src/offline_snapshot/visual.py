"""Compare equal-sized browser screenshots without claiming semantic equivalence."""

from pathlib import Path


def compare(reference, actual, difference=None, tolerance=16, max_changed_ratio=0.01):
    from PIL import Image, ImageChops, ImageStat

    if not 0 <= tolerance <= 255 or not 0 <= max_changed_ratio <= 1:
        raise ValueError("Invalid pixel comparison thresholds")
    with Image.open(reference) as ref, Image.open(actual) as got:
        if ref.size != got.size:
            raise ValueError(f"Screenshot dimensions differ: {ref.size} vs {got.size}")
        diff = ImageChops.difference(ref.convert("RGB"), got.convert("RGB"))
    channels = diff.split()
    maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    hist = maximum.histogram()
    changed = sum(hist[tolerance + 1 :])
    total = diff.width * diff.height
    mae = sum(ImageStat.Stat(diff).mean) / 3
    if difference:
        Path(difference).parent.mkdir(parents=True, exist_ok=True)
        diff.point(lambda value: min(value * 4, 255)).save(difference)
    return {
        "reference": str(reference),
        "actual": str(actual),
        "size": list(diff.size),
        "metric": "fraction of pixels with any RGB channel difference greater than tolerance",
        "channelTolerance": tolerance,
        "maxChangedRatio": max_changed_ratio,
        "changedPixels": changed,
        "changedRatio": changed / total,
        "meanAbsoluteChannelError": mae,
        "passed": changed / total <= max_changed_ratio,
        "difference": str(difference) if difference else None,
    }

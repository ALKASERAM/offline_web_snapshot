import tempfile
import unittest
from pathlib import Path

from PIL import Image

from offline_snapshot.visual import compare


class VisualTests(unittest.TestCase):
    def test_real_pixel_change_fails_and_dimension_mismatch_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder) / "reference.png", Path(folder) / "actual.png"
            Image.new("RGB", (100, 100), "white").save(a)
            self.assertTrue(compare(a, a)["passed"])
            image = Image.new("RGB", (100, 100), "white")
            image.paste("black", (0, 0, 50, 50))
            image.save(b)
            result = compare(a, b)
            self.assertFalse(result["passed"])
            self.assertEqual(result["changedRatio"], 0.25)
            Image.new("RGB", (80, 80), "white").save(b)
            with self.assertRaisesRegex(ValueError, "dimensions differ"):
                compare(a, b)


if __name__ == "__main__":
    unittest.main()

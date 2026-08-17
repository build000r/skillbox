"""Consumer-owned assertion. A probe that edited this byte would be cheating."""
import unittest


class Alpha(unittest.TestCase):
    def test_alpha(self) -> None:
        self.assertEqual(1, 1)

"""Consumer-owned assertion. A probe that edited this byte would be cheating."""
import unittest


class Beta(unittest.TestCase):
    def test_beta(self) -> None:
        self.assertEqual(2, 2)

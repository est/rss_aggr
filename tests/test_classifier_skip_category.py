import unittest

from src.classifier import is_skip_category


class ClassifierSkipCategoryTests(unittest.TestCase):
    def test_skip_category_normalization(self):
        self.assertTrue(is_skip_category("skip"))
        self.assertTrue(is_skip_category(" Skip "))
        self.assertTrue(is_skip_category("'skip'"))
        self.assertTrue(is_skip_category('"skip"'))
        self.assertTrue(is_skip_category("skip (digest)"))
        self.assertFalse(is_skip_category("tech"))


if __name__ == "__main__":
    unittest.main()

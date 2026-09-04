import unittest
from pathlib import Path


class VisualEditorSourceTests(unittest.TestCase):
    def test_block_selector_preserves_selection_and_replaces_block(self):
        source = (Path(__file__).parents[1] / "visual_editor.py").read_text(encoding="utf-8")
        self.assertIn("let savedRange = null", source)
        self.assertIn("const saveSelection", source)
        self.assertIn("const restoreSelection", source)
        self.assertIn("const replaceCurrentBlock", source)
        self.assertIn("blockSelect.onpointerdown = () => saveSelection()", source)
        self.assertIn("replaceCurrentBlock(event.target.value)", source)


if __name__ == "__main__":
    unittest.main()

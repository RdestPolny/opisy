import unittest
from unittest.mock import patch

import visual_editor


class VisualEditorTests(unittest.TestCase):
    def test_reuses_edited_html_from_component_state(self):
        result = type("Result", (), {"html": "<p>Po zmianie</p>"})()
        on_change = lambda: None
        with (
            patch.object(visual_editor.st, "session_state", {"editor": {"html": "<p>Edycja</p>"}}),
            patch.object(visual_editor, "_visual_editor", return_value=result) as component,
        ):
            self.assertEqual(
                visual_editor.visual_html_editor("<p>Start</p>", key="editor", on_change=on_change),
                result.html,
            )
            self.assertEqual(component.call_args.kwargs["data"]["html"], "<p>Edycja</p>")
            self.assertIs(component.call_args.kwargs["on_html_change"], on_change)


if __name__ == "__main__":
    unittest.main()

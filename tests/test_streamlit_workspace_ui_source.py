import unittest
from pathlib import Path


class WorkspaceUiSourceTests(unittest.TestCase):
    def test_does_not_mutate_text_input_key_after_widget_instantiation(self):
        source = (Path(__file__).parents[1] / "app.py").read_text()
        self.assertNotIn('st.session_state.new_description_workspace_name = ""', source)
        self.assertIn('st.session_state.pop("clear_new_description_workspace_name", False)', source)
        self.assertIn('st.session_state.clear_new_description_workspace_name = True', source)


if __name__ == "__main__":
    unittest.main()

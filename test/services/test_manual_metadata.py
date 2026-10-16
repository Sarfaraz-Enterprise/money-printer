import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services import autoflow_studio, scheduler


class ManualMetadataTests(unittest.TestCase):
    def test_manual_fields_reach_render_without_ai_rewrite(self):
        self.assertTrue(autoflow_studio._slot.acquire(blocking=False))
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(scheduler, "STORAGE", Path(directory)),
                patch("app.services.task.start", return_value={}) as start,
                patch("app.services.profile_content.generate_profile_package") as generate,
            ):
                autoflow_studio._run(
                    "mock-task", {"id": "the_afterframe", "name": "The Afterframe"},
                    "My exact subject", "My exact narration", "en-US-GuyNeural", "pexels", [],
                    "dark corridor, analog clock", "My exact description",
                )
        generate.assert_not_called()
        params = start.call_args.args[1]
        self.assertEqual(params.video_subject, "My exact subject")
        self.assertEqual(params.video_script, "My exact narration")
        self.assertEqual(params.video_terms, "dark corridor, analog clock")
        self.assertEqual(params.video_description, "My exact description")
        self.assertFalse(start.call_args.kwargs["auto_publish"])

    def test_manual_studio_exposes_editable_metadata(self):
        profile = {"id": "the_afterframe", "name": "The Afterframe"}
        with patch.object(autoflow_studio, "submit", return_value="mock") as submit:
            app = AppTest.from_string(
                "from webui.dashboard import _studio\n"
                "_studio([{'id':'the_afterframe','name':'The Afterframe'}],False)"
            ).run(timeout=60)
            self.assertFalse(app.exception)
            next(w for w in app.text_input if w.label == "Topic / Video Subject").set_value("My title")
            next(w for w in app.text_area if w.label == "Narration Script").set_value("My script")
            next(w for w in app.text_area if w.label == "Video Description / Caption").set_value("My caption")
            next(w for w in app.text_input if w.label == "Visual Keywords (comma-separated)").set_value("night, corridor")
            next(w for w in app.button if w.label == "Generate Video").click().run(timeout=60)
            self.assertFalse(app.exception)
        self.assertEqual(submit.call_args.args[1:3], ("My title", "My script"))
        self.assertEqual(submit.call_args.args[5], [])
        self.assertEqual(submit.call_args.args[7:], ("night, corridor", "My caption"))


if __name__ == "__main__":
    unittest.main()

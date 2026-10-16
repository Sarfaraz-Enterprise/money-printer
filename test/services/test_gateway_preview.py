import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import env_secrets
from app.services import llm


class GatewayPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="autoflow-gateway-preview-")
        self.addCleanup(self.temporary.cleanup)
        self.dotenv = Path(self.temporary.name) / ".env"
        override = patch.object(env_secrets, "DOTENV_PATH", self.dotenv)
        override.start()
        self.addCleanup(override.stop)

    def test_defaults_do_not_activate_or_initialize_sdk(self):
        with patch.object(llm, "OpenAI") as client:
            result = llm.gateway_configuration_preview()
        client.assert_not_called()
        self.assertEqual(result["base_url"], "https://api.apinex.bond/v1")
        self.assertEqual(result["model"], "free/deepseek-v4-pro-0813")
        self.assertFalse(result["active"])
        self.assertFalse(result["credential_present"])

    def test_root_file_overrides_without_process_values_or_secret_exposure(self):
        self.dotenv.write_text(
            "OPENAI_BASE_URL=https://example.invalid/v1\n"
            "OPENAI_MODEL_NAME=local-test-model\n"
            "OPENAI_API_KEY=private-test-key\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "wrong", "OPENAI_MODEL_NAME": "wrong"}):
            result = llm.gateway_configuration_preview()
        self.assertEqual(result["base_url"], "https://example.invalid/v1")
        self.assertEqual(result["model"], "local-test-model")
        self.assertTrue(result["credential_present"])
        self.assertNotIn("private-test-key", str(result))
        self.assertFalse(result["active"])

    def test_blank_settings_use_defaults_without_interpolation(self):
        self.dotenv.write_text("OPENAI_BASE_URL=\nOPENAI_MODEL_NAME='${MODEL}'\n", encoding="utf-8")
        result = env_secrets.gateway_preview_settings()
        self.assertEqual(result["base_url"], env_secrets.APINEX_BASE_URL)
        self.assertEqual(result["model"], "${MODEL}")


if __name__ == "__main__":
    unittest.main()

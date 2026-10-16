import os
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.config import config, env_secrets
from app.services import llm, material
from app.services.upload_post import UploadPostService

_ALL_ENV = {name: "" for name in env_secrets.ENV_OVERRIDES.values()}


class _PrivateDotenvTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="autoflow-test-env-")
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / ".env"
        target.write_text("")
        self.enterContext(patch.object(env_secrets, "DOTENV_PATH", target))


class EnvSecretsResolveTest(_PrivateDotenvTest):
    def test_dotenv_value_wins_over_process_and_configured_value(self):
        env_secrets.DOTENV_PATH.write_text("ANTHROPIC_API_KEY=file-key\n")
        with patch.dict(os.environ, {**_ALL_ENV, "ANTHROPIC_API_KEY": " env-key "}):
            self.assertEqual(
                env_secrets.resolve("app", "anthropic_api_key", "cfg-key"), "file-key"
            )
            self.assertTrue(env_secrets.is_env_managed("app", "anthropic_api_key"))

    def test_missing_global_file_value_does_not_fall_back_to_config(self):
        with patch.dict(os.environ, _ALL_ENV):
            self.assertEqual(
                env_secrets.resolve("app", "anthropic_api_key", "cfg-key"), None
            )
            self.assertFalse(env_secrets.is_env_managed("app", "anthropic_api_key"))

    def test_unmapped_keys_are_untouched(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
            self.assertEqual(env_secrets.resolve("app", "custom_provider_api_key", "c"), "c")

    def test_stock_media_keys_are_split_into_lists(self):
        env_secrets.DOTENV_PATH.write_text("PEXELS_API_KEY=a, b,,c\n")
        with patch.dict(os.environ, {**_ALL_ENV, "PEXELS_API_KEY": "a, b,,c"}):
            self.assertEqual(
                env_secrets.resolve("app", "pexels_api_keys", []), ["a", "b", "c"]
            )

    def test_resolution_never_writes_into_config(self):
        with (
            patch.dict(config.app, {"upload_post_api_key": ""}),
            patch.dict(os.environ, {**_ALL_ENV, "UPLOAD_POST_API_KEY": "env"}),
        ):
            env_secrets.resolve("app", "upload_post_api_key", "")
            self.assertEqual(config.app["upload_post_api_key"], "")


class ServiceWiringTest(_PrivateDotenvTest):
    def test_material_get_api_key_uses_env(self):
        env_secrets.DOTENV_PATH.write_text("PIXABAY_API_KEY=file-only\n")
        with (
            patch.dict(config.app, {"pixabay_api_keys": ["cfg"]}),
            patch.dict(os.environ, {**_ALL_ENV, "PIXABAY_API_KEY": "env-only"}),
        ):
            self.assertEqual(material.get_api_key("pixabay_api_keys"), "file-only")
        with (
            patch.dict(config.app, {"coverr_api_keys": "cfg"}),
            patch.dict(os.environ, _ALL_ENV),
        ):
            self.assertEqual(material.get_api_key("coverr_api_keys"), "cfg")

    def test_upload_post_uses_env_key_and_snapshots_it(self):
        with (
            patch.dict(config.app, {"upload_post_api_key": "cfg"}),
            patch.dict(os.environ, {**_ALL_ENV, "UPLOAD_POST_API_KEY": "env"}),
        ):
            service = UploadPostService()
            self.assertEqual(service.api_key, "env")
            snapshot = service.snapshot_account()
            self.assertEqual(snapshot["upload_post_api_key"], "env")
        # A queued job keeps the key captured at submission time.
        with patch.dict(os.environ, {**_ALL_ENV, "UPLOAD_POST_API_KEY": "rotated"}):
            self.assertEqual(UploadPostService(snapshot).api_key, "env")

    def test_anthropic_llm_reads_all_settings_from_env(self):
        env_secrets.DOTENV_PATH.write_text("ANTHROPIC_API_KEY=file-anthropic\n")
        class FakeCompletions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                message = types.SimpleNamespace(content="ok")
                return types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=message)]
                )

        fake_completions = FakeCompletions()
        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=fake_completions)
        )
        env = {
            **_ALL_ENV,
            "ANTHROPIC_API_KEY": "env-anthropic",
            "ANTHROPIC_BASE_URL": "https://proxy.example/v1/",
            "ANTHROPIC_WORKSPACE_ID": "wrkspc_env",
            "ANTHROPIC_MODEL": "claude-env-model",
        }
        app_config = {
            "llm_provider": "anthropic",
            "anthropic_api_key": "cfg-key",
            "anthropic_model_name": "cfg-model",
        }
        with (
            patch.dict(os.environ, env),
            patch.object(llm, "OpenAI", return_value=fake_client) as openai_client,
            patch.object(llm, "ChatCompletion", types.SimpleNamespace),
        ):
            llm._generate_response("hi", app_config=app_config)

        kwargs = openai_client.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "file-anthropic")
        self.assertEqual(kwargs["base_url"], "https://proxy.example/v1/")
        self.assertEqual(
            kwargs["default_headers"], {"anthropic-workspace-id": "wrkspc_env"}
        )
        self.assertEqual(fake_completions.kwargs["model"], "claude-env-model")
        # The submitted snapshot itself is not mutated with env secrets.
        self.assertEqual(app_config["anthropic_api_key"], "cfg-key")


if __name__ == "__main__":
    unittest.main()

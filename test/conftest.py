import pytest

from app.config import config, env_secrets
from app.services import accounts, scheduler


@pytest.fixture(autouse=True)
def private_test_dotenv(tmp_path, monkeypatch):
    """Tests must never read or update the real credential vault."""
    target = tmp_path / ".env"
    target.write_text("", encoding="utf-8")
    for name in env_secrets.ENV_OVERRIDES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(env_secrets, "DOTENV_PATH", target)
    monkeypatch.setattr(accounts, "STORAGE_DIR", tmp_path / "accounts")
    monkeypatch.setattr(scheduler, "STORAGE", tmp_path / "scheduler")
    monkeypatch.setitem(config.app, "final_video_export_dir", str(tmp_path / "outputs"))
    return target

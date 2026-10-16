import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.services import accounts


@pytest.fixture
def account_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "STORAGE_DIR", tmp_path / "private-storage")
    return tmp_path / "private-storage"


def test_profile_crud_defaults_and_secret_preservation(account_storage):
    profile = accounts.save_profile(
        {
            "name": "Main channel",
            "enabled": True,
            "enabled_platforms": ["youtube", "facebook", "instagram"],
            "youtube": {"refresh_token": "yt-refresh-secret"},
            "facebook": {"page_id": "page-1", "page_access_token": "meta-secret"},
            "instagram": {"business_account_id": "ig-1"},
        }
    )

    assert profile["schedule"] == {
        "enabled": False,
        "timezone": "Asia/Kolkata",
        "slots": ["05:00", "20:00"],
        "post_times": ["05:00", "20:00"],
        "post_times_ist": ["05:00", "20:00"],
        "niche_style": "afterframe",
        "sub_theme_focus": "",
    }
    assert profile["youtube"]["connected"] is True
    assert profile["facebook"] == {"enabled": True, "connected": True, "page_id": "page-1"}
    assert profile["instagram"] == {
        "enabled": True,
        "connected": True,
        "business_account_id": "ig-1",
    }
    assert "yt-refresh-secret" not in json.dumps(profile)
    assert "meta-secret" not in json.dumps(profile)

    token_path = Path(profile["youtube"]["token_path"])
    assert token_path == account_storage / "tokens" / f"yt_{profile['id']}.json"
    assert json.loads(token_path.read_text(encoding="utf-8"))["refresh_token"] == "yt-refresh-secret"
    assert json.loads((account_storage / "tokens" / f"meta_{profile['id']}.json").read_text())[
        "page_access_token"
    ] == "meta-secret"

    updated = accounts.save_profile(
        {
            "id": profile["id"],
            "name": "Renamed",
            "enabled": True,
            "enabled_platforms": ["youtube"],
            "schedule": {
                "enabled": True,
                "timezone": "Asia/Kolkata",
                "slots": ["06:30"],
                "niche_style": "custom",
                "sub_theme_focus": "city walks",
            },
            "facebook": {"page_id": "page-2", "page_access_token": ""},
        }
    )
    assert updated["name"] == "Renamed"
    assert updated["schedule"]["sub_theme_focus"] == "city walks"
    assert updated["facebook"]["page_id"] == "page-2"
    assert updated["facebook"]["connected"] is True
    assert accounts.list_profiles()[0]["id"] == profile["id"]
    private_listing = accounts.list_profiles(include_secrets=True)[0]
    assert private_listing["youtube"]["refresh_token"] == "yt-refresh-secret"
    assert private_listing["facebook"]["page_access_token"] == "meta-secret"


def test_missing_accounts_returns_empty_and_delete_is_profile_scoped(account_storage):
    assert accounts.list_profiles() == []
    first = accounts.save_profile(
        {"name": "One", "youtube": {"refresh_token": "one-secret"}}
    )
    second = accounts.save_profile(
        {"name": "Two", "facebook": {"page_id": "p2", "page_access_token": "two-secret"}}
    )
    assert accounts.delete_profile(first["id"]) is True
    assert [profile["id"] for profile in accounts.list_profiles()] == [second["id"]]
    assert not Path(first["youtube"]["token_path"]).exists()
    assert (account_storage / "tokens" / f"meta_{second['id']}.json").exists()
    assert accounts.delete_profile(first["id"]) is False


def test_profile_ids_and_schedule_are_validated(account_storage):
    with pytest.raises(ValueError, match="invalid_profile_id"):
        accounts.save_profile({"id": "../../outside"})
    with pytest.raises(ValueError, match="invalid_schedule_timezone"):
        accounts.save_profile({"schedule": {"timezone": "Not/AZone"}})
    with pytest.raises(ValueError, match="invalid_schedule_slots"):
        accounts.save_profile({"schedule": {"slots": ["25:99"]}})
    assert not (account_storage.parent / "outside").exists()


def test_youtube_oauth_url_uses_loopback_state_and_status_is_redacted(
    account_storage, monkeypatch, private_test_dotenv
):
    private_test_dotenv.write_text("GOOGLE_CLIENT_ID=desktop-client-id\nGOOGLE_CLIENT_SECRET=desktop-client-secret\n")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "desktop-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "desktop-client-secret")
    profile = accounts.save_profile({"name": "OAuth"})
    authorization_url = accounts.start_youtube_oauth(profile["id"])
    query = parse_qs(urlparse(authorization_url).query)
    assert query["client_id"] == ["desktop-client-id"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert query["state"][0]
    status = accounts.oauth_status(profile["id"])
    assert status["status"] == "pending"
    assert "secret" not in json.dumps(status).lower()
    assert accounts.delete_profile(profile["id"])

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from app.services import account_dispatcher, accounts, job_store


def _response(status=200, payload=None, headers=None):
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        json=lambda: payload or {},
    )


def _profile(monkeypatch, tmp_path, platforms):
    monkeypatch.setattr(accounts, "STORAGE_DIR", tmp_path / "private-storage")
    return accounts.save_profile(
        {
            "name": "Publisher",
            "enabled": True,
            "enabled_platforms": platforms,
            "youtube": {"refresh_token": "youtube-secret"},
            "facebook": {"page_id": "page-42", "page_access_token": "facebook-secret"},
            "instagram": {
                "business_account_id": "ig-43",
                "access_token": "instagram-secret",
            },
        }
    )


def test_dispatch_succeeds_for_all_platforms_and_history_redacts_tokens(
    tmp_path, monkeypatch
):
    profile = _profile(
        monkeypatch, tmp_path, ["youtube", "instagram", "facebook"]
    )
    monkeypatch.setattr(
        account_dispatcher.accounts,
        "_refresh_youtube_token",
        lambda profile_id: ("access-token", "ok"),
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video payload")

    def post(url, *args, **kwargs):
        if url == "https://www.googleapis.com/upload/youtube/v3/videos":
            return _response(
                200,
                headers={
                    "Location": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=upload-1"
                },
            )
        if url.startswith("https://rupload.facebook.com/"):
            return _response(200, {"success": True})
        if url.endswith("/ig-43/media"):
            return _response(200, {"id": "container-1"})
        if url.endswith("/ig-43/media_publish"):
            return _response(200, {"id": "instagram-media-1"})
        if url.endswith("/page-42/videos"):
            return _response(200, {"id": "facebook-video-1"})
        raise AssertionError(f"unexpected POST URL: {url}")

    monkeypatch.setattr(account_dispatcher.requests, "post", post)
    monkeypatch.setattr(
        account_dispatcher.requests,
        "put",
        lambda *args, **kwargs: _response(201, {"id": "youtube-video-1"}),
    )
    monkeypatch.setattr(
        account_dispatcher.requests,
        "get",
        lambda *args, **kwargs: _response(200, {"status_code": "FINISHED"}),
    )

    results = account_dispatcher.dispatch_video(
        str(video),
        "A title",
        "A caption",
        [profile["id"]],
        tags=["one", "two"],
    )
    assert [(item["platform"], item["status"]) for item in results] == [
        ("youtube", "success"),
        ("facebook", "success"),
        ("instagram", "success"),
    ]
    assert results[0]["url"].endswith("youtube-video-1")
    assert results[1]["url"].endswith("facebook-video-1")
    assert results[2]["url"].endswith("instagram-media-1/")

    history = (accounts.STORAGE_DIR / "dispatch_history.json").read_text(encoding="utf-8")
    assert "youtube-secret" not in history
    assert "facebook-secret" not in history
    assert "instagram-secret" not in history
    assert json.loads(history)[0]["status"] == "success"


def test_dispatch_failure_isolated_and_continues_to_next_platform(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, ["youtube", "facebook"])
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video payload")
    monkeypatch.setattr(
        account_dispatcher.accounts,
        "_refresh_youtube_token",
        lambda profile_id: ("access-token", "ok"),
    )

    def post(url, *args, **kwargs):
        if url == "https://www.googleapis.com/upload/youtube/v3/videos":
            return _response(401, {"error": {"message": "sensitive provider response"}})
        if url.endswith("/page-42/videos"):
            return _response(201, {"id": "facebook-video-2"})
        raise AssertionError(f"unexpected POST URL: {url}")

    monkeypatch.setattr(account_dispatcher.requests, "post", post)
    results = account_dispatcher.dispatch_video(
        str(video), "title", "description", [profile["id"]]
    )
    assert results[0]["status"] == "failed"
    assert results[0]["error_code"] == "api_http_401"
    assert results[1]["status"] == "success"
    assert "sensitive" not in json.dumps(results)


def test_dispatch_returns_safe_configuration_and_file_errors(tmp_path, monkeypatch):
    profile = _profile(monkeypatch, tmp_path, ["facebook"])
    missing_video = tmp_path / "missing.mp4"
    result = account_dispatcher.dispatch_video(
        str(missing_video), "title", "description", [profile["id"]]
    )
    assert result[0]["error_code"] == "video_unavailable"
    assert result[0]["account_id"] == profile["id"]
    assert "facebook-secret" not in json.dumps(result)


def test_publish_outbox_prevents_replaying_success_or_uncertain_results(
    tmp_path, monkeypatch
):
    profile = _profile(monkeypatch, tmp_path, ["facebook"])
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video payload")

    for job_id, result, expected_status in (
        ("published-job", account_dispatcher._success(profile["id"], "facebook", "https://example/post"), "success"),
        ("uncertain-job", account_dispatcher._error("publish_uncertain", account_id=profile["id"], platform="facebook"), "uncertain"),
    ):
        job_store.record_job({
            "job_id": job_id, "profile_id": profile["id"], "status": "running", "stage": "publish",
        })
        with monkeypatch.context() as context:
            dispatch = Mock(return_value=result)
            context.setattr(account_dispatcher, "_dispatch_one", dispatch)
            first = account_dispatcher.dispatch_video(
                str(video), "Title", "Caption", [profile["id"]], job_id=job_id,
            )
            second = account_dispatcher.dispatch_video(
                str(video), "Title", "Caption", [profile["id"]], job_id=job_id,
            )
        assert first[0]["status"] == expected_status
        assert second[0]["status"] == expected_status
        assert dispatch.call_count == 1

    disabled = accounts.save_profile(
        {"id": profile["id"], "enabled": False, "enabled_platforms": ["facebook"]}
    )
    assert disabled["facebook"]["connected"] is True
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video payload")
    result = account_dispatcher.dispatch_video(
        str(video), "title", "description", [profile["id"]]
    )
    assert result[0]["error_code"] == "profile_disabled"

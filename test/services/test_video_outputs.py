import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.controllers.v1 import video as controller
from app.models.exception import HttpException
from app.models.schema import VideoParams
from app.services import task
from app.utils import utils, video_outputs


@pytest.mark.parametrize("subject,expected", [
    ("Mindset Shifts", "Mindset Shifts"),
    ("Top 5 AI Tools in 2026", "Top 5 AI Tools in 2026"),
    (' /\\:*?"<>| ', "Auto_Video"),
    ("", "Auto_Video"),
    ("CON", "_CON"),
    ("AI 🔥 Tech...Trends", "AI 🔥 Tech...Trends"),
    ("a" * 100, "a" * 100),
    ("  A safe subject!  ", "A safe subject!"),
    ("CON.txt", "_CON.txt"),
    ("trailing dots... ", "trailing dots"),
    ("Price/Value", "Price Value"),
    ("ＡＩ Tools", "ＡＩ Tools"),
])
def test_clean_subject(subject, expected):
    assert video_outputs.clean_subject(subject) == expected


def test_relative_root_is_anchored_to_application(tmp_path):
    with (
        patch.dict(task.config.app, {"final_video_export_dir": "my-outputs"}),
        patch.object(utils, "root_dir", return_value=str(tmp_path)),
    ):
        assert video_outputs.output_root() == str(tmp_path / "my-outputs")


def test_parallel_exports_do_not_overwrite_and_skip_gaps(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    root = tmp_path / "outputs"
    with patch.dict(task.config.app, {"final_video_export_dir": str(root)}):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: task._export_final_video(str(source), "Same subject"), range(12)
            ))
        assert len(set(results)) == 12
        assert {Path(file).name for file in results} == {
            "Same subject.mp4",
            *(f"Same subject {index}.mp4" for index in range(2, 13)),
        }
        (root / "manual" / "Same subject 2.mp4").unlink()
        assert Path(task._export_final_video(str(source), "Same subject")).name == "Same subject 13.mp4"
    assert all(Path(file).read_bytes() == b"video" for file in results if Path(file).exists())


@pytest.mark.parametrize("category", video_outputs.OUTPUT_CATEGORIES)
def test_exact_subject_and_duplicate_names_in_each_category(tmp_path, category):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    subject = "Top 5 AI Tools in 2026"
    with patch.dict(task.config.app, {"final_video_export_dir": str(tmp_path / "outputs")}):
        files = [Path(task._export_final_video(str(source), subject, category)) for _ in range(3)]
    assert [file.name for file in files] == [
        f"{subject}.mp4", f"{subject} 2.mp4", f"{subject} 3.mp4",
    ]
    assert all(file.parent.name == category and file.read_bytes() == b"video" for file in files)


def test_case_insensitive_existing_files_and_old_numbered_names(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    directory = tmp_path / "outputs" / "manual"
    directory.mkdir(parents=True)
    existing = directory / "TITLE.MP4"
    existing.write_bytes(b"original")
    with patch.dict(task.config.app, {"final_video_export_dir": str(tmp_path / "outputs")}):
        assert Path(task._export_final_video(str(source), "Title")).name == "Title 2.mp4"
        (directory / "Legacy 1.mp4").write_bytes(b"legacy")
        assert Path(task._export_final_video(str(source), "Legacy")).name == "Legacy 2.mp4"
    assert existing.read_bytes() == b"original"


@pytest.mark.parametrize("category", video_outputs.OUTPUT_CATEGORIES)
def test_final_pipeline_returns_categorized_absolute_path_and_download_url(tmp_path, category):
    root = tmp_path / "outputs"
    params = VideoParams(
        video_subject="AI Tech Trends", output_category=category,
        bgm_type="none", bgm_volume=0,
    )
    task_dir = tmp_path / "tasks" / "example"
    task_dir.mkdir(parents=True)

    def render(**kwargs):
        Path(kwargs["output_file"]).write_bytes(b"rendered video")
        return True

    with (
        patch.dict(task.config.app, {"final_video_export_dir": str(root)}),
        patch.object(utils, "task_dir", return_value=str(task_dir)),
        patch.object(task.video, "combine_videos"),
        patch.object(task.video, "generate_video", side_effect=render),
        patch.object(task.sm.state, "update_task"),
    ):
        finals, _, _ = task.generate_final_videos("example", params, ["clip.mp4"], "audio.mp3", "", 1)
        expected = root / category / "AI Tech Trends.mp4"
        assert finals == [str(expected)]
        assert os.path.isabs(finals[0]) and expected.read_bytes() == b"rendered video"
        assert (task_dir / "final-1.mp4").exists()
        uri = controller._task_file_to_uri(finals[0], "", str(tmp_path / "tasks"), "test")
        assert uri == f"/api/v1/stream/outputs/{category}/AI%20Tech%20Trends.mp4"
        request = SimpleNamespace(headers={"x-task-id": "test"})
        download = asyncio.run(controller.download_video(request, f"outputs/{category}/AI Tech Trends.mp4"))
        assert download.path == str(expected)
        assert download.filename == expected.name
        response = asyncio.run(controller.stream_video(request, f"outputs/{category}/AI Tech Trends.mp4"))
        assert response.headers["Content-Length"] == str(expected.stat().st_size)
        # The stream opens its file before returning the response.
        asyncio.run(response.background())


@pytest.mark.parametrize("unsafe", [
    "outputs/../private.mp4", "outputs/manual/../../private.mp4",
    "outputs/manual/.output.lock", "outputs/not-a-category/file.mp4",
])
def test_output_download_rejects_invalid_paths(unsafe):
    with pytest.raises(HttpException):
        controller._resolve_video_artifact(unsafe, "test")


def test_invalid_category_rejected():
    with pytest.raises(ValidationError):
        VideoParams(video_subject="Title", output_category="../outside")
    with pytest.raises(ValueError):
        video_outputs.category_dir("../outside")


@pytest.mark.parametrize("route", ["stream", "download"])
def test_output_routes_keep_api_authentication(tmp_path, route):
    from fastapi.testclient import TestClient
    from app import asgi

    root = tmp_path / "outputs"
    directory = root / "manual"
    directory.mkdir(parents=True)
    (directory / "Test 1.mp4").write_bytes(b"video")
    with patch.dict(task.config.app, {
        "final_video_export_dir": str(root), "api_key": "output-test-key",
    }):
        client = TestClient(asgi.app)
        url = f"/api/v1/{route}/outputs/manual/Test%201.mp4"
        assert client.get(url).status_code == 401
        response = client.get(url, headers={"x-api-key": "output-test-key"})
        assert response.status_code == 200
        assert response.content == b"video"

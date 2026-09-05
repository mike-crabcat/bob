"""Native video injection — VideoInjection, read_video, capability routing.

Verified shapes come from live probes (2026-09-05): OpenRouter's Responses
API accepts ``{"type": "input_video", "video_url": "data:video/mp4;base64,..."}``
on models whose input modalities include video.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from server.services import model_registry
from server.services.openai_service import _tool_result_messages
from server.services.tools import ImageInjection, VideoInjection
from server.services.workspace_tools import make_workspace_tools

FFMPEG = shutil.which("ffmpeg")


def test_plain_string_result_is_output_row_only():
    rows = _tool_result_messages("just text", "c1", video_supported=True)
    assert rows == [{"type": "function_call_output", "call_id": "c1", "output": "just text"}]


def test_image_injection_appends_input_image_block():
    rows = _tool_result_messages(
        ImageInjection(text="img", data_url="data:image/jpeg;base64,AAA"),
        "c2", video_supported=False)
    assert rows[0] == {"type": "function_call_output", "call_id": "c2", "output": "img"}
    assert rows[1]["role"] == "user"
    assert rows[1]["content"] == [
        {"type": "input_text", "text": "img"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,AAA"},
    ]


def test_image_injection_with_empty_data_url_appends_no_media_block():
    """read_image error returns carry data_url='' — those must not send an
    empty input_image part upstream."""
    rows = _tool_result_messages(
        ImageInjection(text="Error: not a file", data_url=""),
        "c3", video_supported=True)
    assert len(rows) == 1
    assert "Error" in rows[0]["output"]


def test_video_injection_supported_uses_input_video_part():
    rows = _tool_result_messages(
        VideoInjection(text="vid", data_url="data:video/mp4;base64,AAA", path="/w/x.mp4"),
        "c4", video_supported=True)
    assert rows[1]["content"][1] == {"type": "input_video", "video_url": "data:video/mp4;base64,AAA"}


def test_video_injection_unsupported_without_path_degrades_to_note():
    rows = _tool_result_messages(
        VideoInjection(text="vid", data_url="data:video/mp4;base64,AAA", path=""),
        "c5", video_supported=False)
    assert len(rows) == 1
    assert "cannot watch video" in rows[0]["output"]
    assert "ffmpeg" in rows[0]["output"]


def test_video_injection_unsupported_with_path_shows_first_frame():
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        clip = Path(td) / "clip.mp4"
        subprocess.run([
            FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=2:size=320x240:rate=5",
            "-pix_fmt", "yuv420p", str(clip),
        ], check=True)
        rows = _tool_result_messages(
            VideoInjection(text="vid", data_url="data:video/mp4;base64,AAA", path=str(clip)),
            "c6", video_supported=False)
        assert len(rows) == 2
        assert "first frame" in rows[0]["output"]
        part = rows[1]["content"][1]
        assert part["type"] == "input_image"
        assert part["image_url"].startswith("data:image/jpeg;base64,")
        # frame cache lands beside the clip (prompt_assembler convention)
        assert Path(str(clip) + ".frame.jpg").exists()


def test_supports_video_reads_models_yaml_flag(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "video_input:\n  some/video-model: true\n  other/model: false\n")
    assert model_registry.supports_video(tmp_path, "some/video-model") is True
    assert model_registry.supports_video(tmp_path, "other/model") is False
    assert model_registry.supports_video(tmp_path, "unlisted/model") is False
    # non-dict garbage degrades to False, never raises
    (tmp_path / "models.yaml").write_text("video_input: banana\n")
    assert model_registry.supports_video(tmp_path, "some/video-model") is False


async def test_read_video_requires_video_suffix(ctx, tmp_path):
    ctx.settings.harness.workspace_dir = tmp_path
    (tmp_path / "x.txt").write_text("nope")
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    result = await tools["read_video"].handler(path="x.txt")
    assert isinstance(result, str) and "not a supported video format" in result


async def test_read_video_missing_file(ctx, tmp_path):
    ctx.settings.harness.workspace_dir = tmp_path
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    result = await tools["read_video"].handler(path="ghost.mp4")
    assert isinstance(result, str) and "is not a file" in result


async def test_read_video_returns_injection_with_path(ctx, tmp_path):
    ctx.settings.harness.workspace_dir = tmp_path
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    result = await tools["read_video"].handler(path="clip.mp4")
    assert isinstance(result, VideoInjection)
    assert result.data_url.startswith("data:video/mp4;base64,")
    assert result.path == str(tmp_path / "clip.mp4")


async def test_read_video_one_per_tool_set(ctx, tmp_path):
    """Second video in the same tool set is rejected — native video is one
    per turn by design."""
    ctx.settings.harness.workspace_dir = tmp_path
    (tmp_path / "a.mp4").write_bytes(b"fakeA")
    (tmp_path / "b.mp4").write_bytes(b"fakeB")
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    first = await tools["read_video"].handler(path="a.mp4")
    assert isinstance(first, VideoInjection)
    second = await tools["read_video"].handler(path="b.mp4")
    assert isinstance(second, str) and "one video per turn" in second


async def test_read_video_rejects_oversized(ctx, tmp_path, monkeypatch):
    from server.services import workspace_tools
    monkeypatch.setattr(workspace_tools, "_VIDEO_MAX_BYTES", 8)
    ctx.settings.harness.workspace_dir = tmp_path
    (tmp_path / "big.mp4").write_bytes(b"0123456789abcdef")
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    result = await tools["read_video"].handler(path="big.mp4")
    assert isinstance(result, str) and "over the 8-byte cap" in result


async def test_read_video_blocks_outside_workspace(ctx, tmp_path):
    ctx.settings.harness.workspace_dir = tmp_path
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    with pytest.raises(ValueError):
        await tools["read_video"].handler(path="/etc/passwd")
    with pytest.raises(ValueError):
        await tools["read_video"].handler(path="../escape.mp4")

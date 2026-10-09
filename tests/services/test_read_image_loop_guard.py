"""read_image re-read guard + image keep window (2026-10-09 Leeming beer
re-judge looped 46 reads over 5 photos)."""

from types import SimpleNamespace

from server.services import tool_loop_folding as tlf
from server.services.workspace_tools import make_workspace_tools

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
       b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


async def test_third_read_of_same_image_is_refused(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    ctx = SimpleNamespace(settings=SimpleNamespace(
        harness=SimpleNamespace(workspace_dir=tmp_path)), db=None)
    read = next(t for t in make_workspace_tools(ctx) if t.name == "read_image")
    first = await read.handler(path="a.png")
    second = await read.handler(path="a.png")
    third = await read.handler(path="a.png")
    assert first.data_url and second.data_url
    assert not third.data_url and "already viewed" in third.text


def test_image_window_holds_a_five_photo_set():
    msgs = [{"type": "function_call_output",
             "output": [{"type": "input_text", "text": f"img {i}"},
                        {"type": "input_image", "image_url": "data:x"}]}
            for i in range(6)]
    assert tlf.fold_aged_image_outputs(msgs, keep_last=tlf.IMAGE_KEEP_LAST) == 0

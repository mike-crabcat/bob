"""Published-files route (Printful design publishing, 2026-08-26).

Printful ingests designs only by fetching a public URL; Bob serves its
workspace publish directory at an unguessable token path through the
Tailscale Funnel. These tests pin the security properties: wrong token is
indistinguishable from missing file, no traversal, allow-listed types only.
"""

from __future__ import annotations

import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from server.main import create_app
from server.config import Settings


@pytest.fixture
async def client(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "skills" / "printful").mkdir(parents=True)
    publish = workspace / "published-files"
    publish.mkdir()
    token = secrets.token_urlsafe(24)
    (workspace / "skills" / "printful" / "publish_token").write_text(token)
    (publish / "design.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")

    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "cfg")
    settings.harness.workspace_dir = workspace
    app = create_app(settings=settings)
    # ASGITransport does not run the lifespan; the route only needs state.
    app.state.settings = settings
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, token


async def test_published_file_served_with_good_token(client):
    c, token = client
    r = await c.get(f"/files/{token}/design.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")

    head = await c.head(f"/files/{token}/design.png")
    assert head.status_code == 200, "HEAD must work (the skill's publish check)"


async def test_wrong_token_is_plain_404(client):
    c, _ = client
    r = await c.get("/files/not-the-token/design.png")
    assert r.status_code == 404
    # And indistinguishable from a missing file under the right token.
    c2, token = client
    assert (await c2.get(f"/files/{token}/nope.png")).status_code == 404


async def test_no_token_file_configured_means_all_404(client, tmp_path):
    c, token = client
    # remove the token file: the route must close, and rotation takes
    # effect with no restart (read per request).
    (tmp_path / "workspace" / "skills" / "printful" / "publish_token").unlink()
    assert (await c.get(f"/files/{token}/design.png")).status_code == 404


async def test_disallowed_types_and_traversal_rejected(client):
    c, token = client
    assert (await c.get(f"/files/{token}/design.exe")).status_code == 404
    assert (await c.get(f"/files/{token}/..%2F..%2Fapi_secret")).status_code == 404
    assert (await c.get(f"/files/{token}/sub%2Fdir.png")).status_code == 404

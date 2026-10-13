"""Credential query params never reach the logs (2026-10-13)."""

from __future__ import annotations

import json
import logging

from server.structured_logging import StructuredFormatter, redact_secrets


def test_redacts_secret_params_keeps_rest():
    line = ('127.0.0.1:1 - "GET /dashboard/api/goals/abc?secret=zH8Rj59b&x=1 HTTP/1.1" 200')
    out = redact_secrets(line)
    assert "zH8Rj59b" not in out
    assert "?secret=[redacted]&x=1 HTTP/1.1" in out
    assert redact_secrets("/a?TOKEN=abc") == "/a?TOKEN=[redacted]"
    assert redact_secrets("/a?keyboard=qwerty") == "/a?keyboard=qwerty"   # not a key=
    assert redact_secrets("no params here") == "no params here"


def test_formatter_redacts_uvicorn_access_args():
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/dashboard/api/x?secret=s3cr3t", "1.1", 200), None)
    out = json.loads(StructuredFormatter().format(rec))
    assert "s3cr3t" not in out["message"] and "[redacted]" in out["message"]

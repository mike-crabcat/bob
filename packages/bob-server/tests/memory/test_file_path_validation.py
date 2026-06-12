"""Tests for file_path validation in claim extraction."""

from __future__ import annotations

from bob_server.services.memory.claim_service import (
    _is_valid_file_path,
    _invalid_file_entities,
)


class TestIsValidFilePath:
    def test_rejects_empty_string(self):
        assert not _is_valid_file_path("")

    def test_rejects_vague_workspace(self):
        assert not _is_valid_file_path("workspace")

    def test_rejects_vague_project(self):
        assert not _is_valid_file_path("project")

    def test_rejects_vague_root(self):
        assert not _is_valid_file_path("root")

    def test_rejects_vague_directory(self):
        assert not _is_valid_file_path("directory")

    def test_rejects_vague_folder(self):
        assert not _is_valid_file_path("folder")

    def test_rejects_vague_repo(self):
        assert not _is_valid_file_path("repo")

    def test_rejects_vague_local(self):
        assert not _is_valid_file_path("local")

    def test_rejects_vague_home(self):
        assert not _is_valid_file_path("home")

    def test_accepts_relative_path_with_slash(self):
        assert _is_valid_file_path("docs/itinerary.md")

    def test_accepts_nested_relative_path(self):
        assert _is_valid_file_path("src/components/App.tsx")

    def test_accepts_url(self):
        assert _is_valid_file_path("https://docs.google.com/spreadsheets/d/abc123")

    def test_accepts_http_url(self):
        assert _is_valid_file_path("http://example.com/file.pdf")

    def test_accepts_s3_url(self):
        assert _is_valid_file_path("s3://bucket-name/key/file.csv")

    def test_accepts_gs_url(self):
        assert _is_valid_file_path("gs://bucket/data.json")

    def test_accepts_bare_filename_with_extension(self):
        assert _is_valid_file_path("itinerary.md")

    def test_accepts_bare_filename_with_long_extension(self):
        assert _is_valid_file_path("data.xlsx")

    def test_accepts_dotfile(self):
        assert _is_valid_file_path(".env")

    def test_rejects_bare_dot(self):
        assert not _is_valid_file_path(".")

    def test_rejects_double_dot(self):
        assert not _is_valid_file_path("..")

    def test_rejects_whitespace_only(self):
        assert not _is_valid_file_path("   ")

    def test_accepts_path_with_surrounding_quotes(self):
        assert _is_valid_file_path('"docs/itinerary.md"')

    def test_accepts_path_with_surrounding_single_quotes(self):
        assert _is_valid_file_path("'src/main.py'")

    def test_case_insensitive_rejection(self):
        assert not _is_valid_file_path("Workspace")
        assert not _is_valid_file_path("PROJECT")
        assert not _is_valid_file_path("Root")

    def test_rejects_project_root(self):
        assert not _is_valid_file_path("project root")

    def test_rejects_workspace_root(self):
        assert not _is_valid_file_path("workspace root")


class TestInvalidFileEntities:
    def test_missing_path_returns_id(self):
        result = _invalid_file_entities({"file-abc"}, {})
        assert result == {"file-abc"}

    def test_valid_path_is_not_invalid(self):
        result = _invalid_file_entities(set(), {"file-abc": "docs/readme.md"})
        assert result == set()

    def test_vague_path_is_invalid(self):
        result = _invalid_file_entities(set(), {"file-abc": "workspace"})
        assert result == {"file-abc"}

    def test_url_path_is_valid(self):
        result = _invalid_file_entities(set(), {"file-abc": "https://docs.google.com/..."})
        assert result == set()

    def test_mixed_valid_and_invalid(self):
        result = _invalid_file_entities(
            {"file-no-path"},
            {"file-good": "src/app.py", "file-bad": "project"},
        )
        assert result == {"file-no-path", "file-bad"}

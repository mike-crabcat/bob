"""models.yaml addenda — model-specific guidance appended at run_turn."""

from types import SimpleNamespace

from server.services import llm_dispatch as d


def _settings(tmp_path):
    (tmp_path / "models.yaml").write_text(
        "aliases:\n  luna: gpt-6-luna\naddenda:\n  luna: |\n    - fix small things yourself\n")
    return SimpleNamespace(config_dir=tmp_path)


def test_addendum_applied_once_for_the_serving_model(tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_MODEL_ADDENDA", "on")
    msgs = [{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}]
    d._apply_model_addendum(msgs, "gpt-6-luna", _settings(tmp_path))
    d._apply_model_addendum(msgs, "gpt-6-luna", _settings(tmp_path))
    assert msgs[0]["content"].count("## Notes for this model") == 1
    assert "fix small things yourself" in msgs[0]["content"]


def test_other_models_and_flag_off_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_MODEL_ADDENDA", "on")
    msgs = [{"role": "system", "content": "base"}]
    d._apply_model_addendum(msgs, "z-ai/glm-5.3-flash", _settings(tmp_path))
    assert msgs[0]["content"] == "base"
    monkeypatch.setenv("BOB_MODEL_ADDENDA", "off")
    d._apply_model_addendum(msgs, "gpt-6-luna", _settings(tmp_path))
    assert msgs[0]["content"] == "base"

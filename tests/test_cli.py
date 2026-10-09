import json
import pytest

from upgrade_resolver.cli import main


def test_invalid_interval_rejected_before_retrieval(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--project", "Orbit", "--current-version", "2", "--target-version", "1"])
    assert exc.value.code == 2
    assert "newer" in capsys.readouterr().err


def test_offline_empty_cache_emits_reviewable_json(tmp_path, capsys):
    code = main(["--project", "Orbit", "--current-version", "1", "--target-version", "2",
                 "--offline", "--cache-dir", str(tmp_path), "--web-search", "none"])
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "unresolved"
    assert output["network_requests"] == 0
    assert any("Offline cache miss" in warning for warning in output["warnings"])


def test_output_file_contains_json(tmp_path, capsys):
    path = tmp_path / "results" / "orbit.json"
    main(["--project", "Orbit", "--current-version", "1", "--target-version", "2",
          "--offline", "--cache-dir", str(tmp_path / "cache"), "--web-search", "none", "--output", str(path)])
    assert json.loads(path.read_text())["status"] == "unresolved"
    assert capsys.readouterr().out == ""


def test_decision_scorer_uses_the_scorer_stage_settings(tmp_path, monkeypatch, capsys):
    import os
    for name in list(os.environ):
        if name.startswith("DECISION_"):
            monkeypatch.delenv(name)
    seen = {}
    monkeypatch.setattr("upgrade_pipeline.models.local.load_d1",
                        lambda model, revision, *, offline=False: seen.update(model=model, offline=offline) or object())
    monkeypatch.setattr("upgrade_resolver.cli.resolve", lambda *a, **k: seen.update(backend=k["decision_backend"]) or
                        {"status": "partial", "versions": [], "documentation_milestones": []})
    assert main(["--project", "Orbit", "--current-version", "1", "--target-version", "2", "--scorer", "decision",
                 "--offline", "--cache-dir", str(tmp_path)]) == 2
    assert seen["model"] == "LiquidAI/d1-3B" and seen["offline"] is True and seen["backend"] is not None
    assert "model: scorer: d1 LiquidAI/d1-3B@" in capsys.readouterr().err
    monkeypatch.setenv("DECISION_SCORER_BASE_URL", "https://decide/v1")  # remote backend cannot run offline
    assert main(["--project", "Orbit", "--current-version", "1", "--target-version", "2", "--scorer", "decision",
                 "--offline", "--cache-dir", str(tmp_path), "--scorer-decision-model", "jev-latest"]) == 1

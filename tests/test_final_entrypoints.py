from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_build_paper_final_runs_only_when_blockers_are_empty(tmp_path, monkeypatch):
    module = _load_script("test_build_paper_module", "scripts/build_paper.py")
    monkeypatch.setattr(module, "collect_blockers", lambda repo, mode: [])
    monkeypatch.setattr(module, "generate_final_assets", lambda repo: {})
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/fake/{name}")

    def fake_run(command, cwd=None, capture_output=None, text=None):
        build_dir = Path(cwd) / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        target = "main" if "main.tex" in command else "ai_details"
        (build_dir / f"{target}.pdf").write_bytes(b"%PDF-1.4 fake")
        (build_dir / f"{target}.log").write_text("", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    code = module.main(["--mode", "final", "--repo", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "paper" / "build" / "main.pdf").is_file()


def test_build_paper_final_still_blocks_when_blockers_exist(tmp_path, monkeypatch):
    module = _load_script("test_build_paper_blocked_module", "scripts/build_paper.py")
    monkeypatch.setattr(module, "collect_blockers", lambda repo, mode: ["still blocked"])
    code = module.main(["--mode", "final", "--repo", str(tmp_path)])
    assert code == 5


def test_prepare_submission_final_runs_only_when_blockers_are_empty(tmp_path, monkeypatch):
    module = _load_script("test_prepare_submission_module", "scripts/prepare_submission.py")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(module, "collect_blockers", lambda repo, mode: [])
    monkeypatch.setattr(module, "create_staging", lambda repo: staging)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    code = module.main(["--mode", "final", "--repo", str(tmp_path)])
    assert code == 0


def test_prepare_submission_final_still_blocks_when_blockers_exist(tmp_path, monkeypatch):
    module = _load_script("test_prepare_submission_blocked_module", "scripts/prepare_submission.py")
    monkeypatch.setattr(module, "collect_blockers", lambda repo, mode: ["still blocked"])
    code = module.main(["--mode", "final", "--repo", str(tmp_path)])
    assert code == 5

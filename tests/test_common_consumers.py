"""scripts/common_consumers.py: which repos a change to common needs rebuilt.
Builds a throwaway common git repo and consumer repos in tmp_path."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "common_consumers.py"
spec = importlib.util.spec_from_file_location("common_consumers", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path):
    root = tmp_path
    common = root / "common"
    common.mkdir()
    _git(common, "init", "-q")
    _write(common / "pyproject.toml", "[project]\nname='common-utils'\n")
    _write(common / "common_utils/__init__.py", "")
    _write(common / "common_utils/log_context.py", "X = 1\n")
    _write(common / "common_utils/stealth/__init__.py", "")
    _write(common / "common_utils/stealth/proxy_runner.py", "from common_utils.log_context import X\n")
    _write(common / "common_utils/dealvant/__init__.py", '"""pkg"""\n')
    _write(common / "common_utils/dealvant/store.py", "Y = 1\n")
    _write(common / "common_utils/dealvant/products.py", "from common_utils.dealvant.store import Y\n")
    _write(common / "common_utils/dealvant/requests.py", "from .store import Y\n")
    _write(common / "orchestration/pipeline.yaml", "sequence: []\n")
    _write(common / "Dockerfile.scraper-base", "FROM python\n")
    base = _commit(common, "base")

    repos = {
        "dealnews1": {"run.py": "from common_utils.stealth.proxy_runner import run\n"},
        "dealmoon1": {"run.py": "import common_utils.stealth.proxy_runner\n"},
        "amazonnew": {"pipe.py": "from common_utils.dealvant import products\n", "scripts/job.sh": "python -m common_utils.log_context\n"},
        "reviewgate": {"app.py": "from common_utils.dealvant.requests import enqueue\n", "tests/t.py": "from common_utils.stealth import proxy_runner\n"},
        "autopub": {"w.py": "from common_utils.dealvant import store as s\n"},
        "aistage": {"api.py": "from common_utils.log_context import X\n"},
    }
    for repo, files in repos.items():
        (root / repo / ".git").mkdir(parents=True)
        for name, text in files.items():
            _write(root / repo / name, text)

    state = root / "state"
    state.mkdir()
    for repo in repos:
        (state / repo).write_text(base)
    return root, common, state


def _assess(root, common, state, repo):
    head = _git(common, "rev-parse", "HEAD")
    return cc.assess(repo, common_dir=common, repos_root=root, state_dir=state, head=head, modules=cc.common_modules(common))


def _rebuilds(root, common, state, host="vps1"):
    return {repo for repo in cc.CONSUMERS[host] if _assess(root, common, state, repo)[0]}


def test_only_users_of_a_changed_module_rebuild(world) -> None:
    root, common, state = world
    _write(common / "common_utils/dealvant/store.py", "Y = 2\n")
    _commit(common, "store")
    # products and requests import store (absolute and relative); autopub
    # imports store directly. The scrapers don't touch dealvant.
    assert _rebuilds(root, common, state) == {"amazonnew", "reviewgate", "autopub"}
    rebuild, reason = _assess(root, common, state, "reviewgate")
    assert rebuild and "common_utils.dealvant.requests" in reason


def test_transitive_importers_count(world) -> None:
    root, common, state = world
    _write(common / "common_utils/log_context.py", "X = 2\n")
    _commit(common, "log")
    # stealth.proxy_runner imports log_context -> the scrapers; amazonnew via
    # its shell script reference; aistage directly. Tests don't count.
    assert _rebuilds(root, common, state) == {"dealnews1", "dealmoon1", "amazonnew"}
    assert _assess(root, common, state, "aistage")[0]
    assert not _assess(root, common, state, "reviewgate")[0]


def test_package_init_change_affects_its_users(world) -> None:
    root, common, state = world
    _write(common / "common_utils/dealvant/__init__.py", '"""pkg v2"""\n')
    _commit(common, "init")
    assert _rebuilds(root, common, state) == {"amazonnew", "reviewgate", "autopub"}


def test_changes_outside_common_utils_rebuild_nothing(world) -> None:
    root, common, state = world
    _write(common / "orchestration/pipeline.yaml", "sequence: [x]\n")
    _write(common / "common_utils/dealvant/ROUNDUPS.md", "docs\n")
    _write(common / "tests/test_x.py", "pass\n")
    _commit(common, "docs and schedule")
    assert _rebuilds(root, common, state) == set()
    assert "nothing it ships changed" in _assess(root, common, state, "dealnews1")[1]


def test_safe_defaults(world) -> None:
    root, common, state = world
    _write(common / "pyproject.toml", "[project]\nname='common-utils'\nversion='2'\n")
    _commit(common, "packaging")
    assert _rebuilds(root, common, state) == set(cc.CONSUMERS["vps1"])

    (state / "dealnews1").unlink()
    assert "no record" in _assess(root, common, state, "dealnews1")[1]
    (state / "dealmoon1").write_text("0" * 40)
    assert "isn't in this checkout" in _assess(root, common, state, "dealmoon1")[1]


def test_scraper_base_inputs_rebuild_only_its_image(world) -> None:
    root, common, state = world
    _write(common / "Dockerfile.scraper-base", "FROM python:3.11\n")
    _commit(common, "base image")
    assert _rebuilds(root, common, state) == {"amazonnew"}


def test_up_to_date_and_non_python_files_in_package(world) -> None:
    root, common, state = world
    head = _git(common, "rev-parse", "HEAD")
    (state / "autopub").write_text(head)
    assert _assess(root, common, state, "autopub") == (False, f"already built with common {head[:7]}")
    _write(common / "common_utils/stealth/fingerprints.json", "{}\n")
    _commit(common, "data file")
    rebuild, reason = _assess(root, common, state, "aistage")
    assert rebuild and "non-Python file" in reason


def test_cli_names_only(world, capsys) -> None:
    root, common, state = world
    _write(common / "common_utils/dealvant/store.py", "Y = 3\n")
    _commit(common, "store")
    cc.main(["--host", "vps1", "--repos-root", str(root), "--state-dir", str(state), "--names-only"])
    assert capsys.readouterr().out.split() == ["amazonnew", "reviewgate", "autopub"]  # rebuild order

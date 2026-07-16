import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import typer

from yuanclaw.cli import commands


def _bridge_layout(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source = repo / "bridge"
    source.mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps({"name": "bridge", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "index.ts").write_text("export {};", encoding="utf-8")
    fake_commands = repo / "yuanclaw" / "cli" / "commands.py"
    fake_commands.parent.mkdir(parents=True)
    monkeypatch.setattr(commands, "__file__", str(fake_commands))

    install = tmp_path / "runtime" / "bridge"
    monkeypatch.setattr(
        "yuanclaw.config.paths.get_bridge_install_dir",
        lambda: install,
    )
    return source, install


def _install_signature(source: Path) -> dict[str, str]:
    return {
        "version": "0.1.0",
        "lock_sha256": hashlib.sha256(
            (source / "package-lock.json").read_bytes()
        ).hexdigest(),
    }


def test_bridge_installer_builds_in_staging_before_replacing_existing(
    tmp_path,
    monkeypatch,
) -> None:
    _source, install = _bridge_layout(tmp_path, monkeypatch)
    install.mkdir(parents=True)
    (install / "old-marker").write_text("keep until ready", encoding="utf-8")
    calls = []

    monkeypatch.setattr(commands.shutil, "which", lambda name: f"C:/{name}.cmd")

    def run(args, **kwargs):
        calls.append((list(args), kwargs.get("cwd")))
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="v20.11.0\n")
        if args[1:] == ["run", "build"]:
            dist = Path(kwargs["cwd"]) / "dist"
            dist.mkdir()
            (dist / "index.js").write_text("built", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(commands.subprocess, "run", run)

    result = commands._get_bridge_dir()

    assert result == install
    assert (install / "dist" / "index.js").is_file()
    assert not (install / "old-marker").exists()
    assert json.loads(
        (install / ".yuanclaw-bridge-install.json").read_text(encoding="utf-8")
    )["version"] == "0.1.0"
    assert any(args[1:] == ["ci"] for args, _cwd in calls)
    assert not any(args[1:] == ["install"] for args, _cwd in calls)


def test_bridge_installer_failure_keeps_previous_install(tmp_path, monkeypatch) -> None:
    _source, install = _bridge_layout(tmp_path, monkeypatch)
    install.mkdir(parents=True)
    marker = install / "old-marker"
    marker.write_text("still valid", encoding="utf-8")
    monkeypatch.setattr(commands.shutil, "which", lambda name: f"C:/{name}.cmd")

    def run(args, **_kwargs):
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="v20.11.0\n")
        raise subprocess.CalledProcessError(1, args, stderr=b"build failed")

    monkeypatch.setattr(commands.subprocess, "run", run)

    with pytest.raises(typer.Exit):
        commands._get_bridge_dir()

    assert marker.read_text(encoding="utf-8") == "still valid"
    assert list(install.parent.glob(".bridge.staging-*")) == []


def test_bridge_installer_reuses_matching_manifest_without_node(
    tmp_path,
    monkeypatch,
) -> None:
    source, install = _bridge_layout(tmp_path, monkeypatch)
    (install / "dist").mkdir(parents=True)
    (install / "dist" / "index.js").write_text("built", encoding="utf-8")
    (install / ".yuanclaw-bridge-install.json").write_text(
        json.dumps(_install_signature(source)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        commands.shutil,
        "which",
        lambda _name: (_ for _ in ()).throw(AssertionError("Node lookup is unnecessary")),
    )

    assert commands._get_bridge_dir() == install

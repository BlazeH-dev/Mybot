from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from nanobot.officecli_runtime import (
    OFFICECLI_DIR_ENV,
    OFFICECLI_SKIP_UPDATE_ENV,
    OfficeCliBootstrapError,
    _execution_env,
    ensure_officecli,
    load_officecli_contract,
    select_officecli_asset,
)
from nanobot.skills.officecli.scripts.officecli_backend import OfficeCliInfo, _OfficeCliRunner


def _contract_for(payload: bytes) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "validated_version": "1.0.135",
        "release_url": "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.0.135",
        "assets": {
            "mac-arm64": {"name": "officecli-mac-arm64", "sha256": digest},
            "linux-alpine-x64": {
                "name": "officecli-linux-alpine-x64",
                "sha256": digest,
            },
            "win-x64": {"name": "officecli-win-x64.exe", "sha256": digest},
        },
    }


def test_packaged_contract_selects_supported_platform_assets() -> None:
    contract = load_officecli_contract()

    mac = select_officecli_asset(contract, system="Darwin", machine="arm64")
    alpine = select_officecli_asset(
        contract,
        system="Linux",
        machine="x86_64",
        alpine=True,
    )
    windows = select_officecli_asset(contract, system="Windows", machine="AMD64")

    assert mac.platform_key == "mac-arm64"
    assert mac.name == "officecli-mac-arm64"
    assert mac.version == "1.0.135"
    assert mac.url.endswith("/v1.0.135/officecli-mac-arm64")
    assert alpine.platform_key == "linux-alpine-x64"
    assert windows.name == "officecli-win-x64.exe"


def test_packaged_contract_keeps_version_capabilities_and_policy_boundaries() -> None:
    contract = load_officecli_contract()

    assert contract["provider"] == "officecli"
    assert contract["validated_version"] == "1.0.135"
    assert contract["allowed_batch_operations"] == ["add", "set"]
    assert contract["runtime_environment"]["OFFICECLI_NO_AUTO_RESIDENT"] == "1"
    assert {"raw-set", "plugins", "mcp", "watch", "install"}.issubset(
        contract["capabilities"]
    )
    assert "raw-set" in contract["policy_hints"]["ask"]
    assert all(len(asset["sha256"]) == 64 for asset in contract["assets"].values())


def test_officecli_skill_routes_python_requests_to_office_python() -> None:
    root = Path(__file__).resolve().parents[2]
    skill_text = (root / "nanobot" / "skills" / "officecli" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (root / "nanobot" / "skills" / "officecli" / "references" / "officecli-runtime.json").read_text(
            encoding="utf-8"
        )
    )

    assert "`office-python`" in skill_text
    assert "office-automation" not in skill_text
    assert manifest["validated_version"] == "1.0.135"


def test_unsupported_platform_fails_closed() -> None:
    with pytest.raises(OfficeCliBootstrapError, match="Unsupported OfficeCLI operating system"):
        select_officecli_asset(
            load_officecli_contract(),
            system="FreeBSD",
            machine="x86_64",
        )


def test_ensure_officecli_downloads_verifies_and_reuses(tmp_path: Path) -> None:
    payload = b"pinned-officecli-binary"
    downloads: list[str] = []

    def download(url: str, destination: Path) -> None:
        downloads.append(url)
        destination.write_bytes(payload)

    binary = ensure_officecli(
        contract=_contract_for(payload),
        runtime_dir=tmp_path,
        downloader=download,
        system="Darwin",
        machine="arm64",
    )
    reused = ensure_officecli(
        contract=_contract_for(payload),
        runtime_dir=tmp_path,
        downloader=download,
        system="Darwin",
        machine="arm64",
    )

    assert binary == reused
    assert binary.read_bytes() == payload
    assert len(downloads) == 1
    assert binary.stat().st_mode & 0o111


def test_ensure_officecli_replaces_tampered_cache(tmp_path: Path) -> None:
    payload = b"validated-binary"
    download_count = 0

    def download(_url: str, destination: Path) -> None:
        nonlocal download_count
        download_count += 1
        destination.write_bytes(payload)

    kwargs = {
        "contract": _contract_for(payload),
        "runtime_dir": tmp_path,
        "downloader": download,
        "system": "Darwin",
        "machine": "arm64",
    }
    binary = ensure_officecli(**kwargs)
    binary.write_bytes(b"auto-updated-or-corrupted")

    repaired = ensure_officecli(**kwargs)

    assert repaired.read_bytes() == payload
    assert download_count == 2


def test_checksum_mismatch_never_installs_binary(tmp_path: Path) -> None:
    def download(_url: str, destination: Path) -> None:
        destination.write_bytes(b"wrong-binary")

    with pytest.raises(OfficeCliBootstrapError, match="checksum mismatch"):
        ensure_officecli(
            contract=_contract_for(b"expected-binary"),
            runtime_dir=tmp_path,
            downloader=download,
            system="Darwin",
            machine="arm64",
        )

    assert not list(tmp_path.rglob("officecli-mac-arm64"))
    assert not list(tmp_path.rglob("*.part"))


def test_launcher_always_disables_upstream_auto_update(monkeypatch) -> None:
    monkeypatch.setenv(OFFICECLI_SKIP_UPDATE_ENV, "0")

    assert _execution_env()[OFFICECLI_SKIP_UPDATE_ENV] == "1"


def test_officecli_runner_keeps_runtime_cache_outside_isolated_home(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv(OFFICECLI_DIR_ENV, str(runtime_dir))
    runner = _OfficeCliRunner(OfficeCliInfo(binary="officecli", version="1.0.135"))
    try:
        assert runner.env[OFFICECLI_DIR_ENV] == str(runtime_dir)
        assert runner.env["HOME"] != str(Path.home())
        assert runner.env["OFFICECLI_NO_AUTO_RESIDENT"] == "1"
    finally:
        runner.close()


def test_officecli_runner_does_not_wait_for_child_holding_output_descriptors() -> None:
    child_code = "import time; time.sleep(2)"
    parent_code = (
        "import json,subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "print(json.dumps({'success': True}))"
    )
    runner = _OfficeCliRunner(
        OfficeCliInfo(binary=sys.executable, version="test"),
        timeout_seconds=1,
    )
    started = time.monotonic()
    try:
        result = runner.run(["-c", parent_code])
    finally:
        runner.close()

    assert result["stdout"] == {"success": True}
    assert time.monotonic() - started < 1


def test_officecli_runner_accepts_completed_png_and_kills_lingering_process(
    tmp_path: Path,
) -> None:
    output = tmp_path / "preview.png"
    parent_code = (
        "import pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_bytes(b'\\x89PNG\\r\\n\\x1a\\npreview'); "
        "time.sleep(5)"
    )
    runner = _OfficeCliRunner(
        OfficeCliInfo(binary=sys.executable, version="test"),
        timeout_seconds=1,
    )
    try:
        result = runner.run(
            ["-c", parent_code, str(output)],
            completed_output_path=output,
        )
    finally:
        runner.close()

    assert result["exit_code"] == 124
    assert result["stdout"]["success"] is True
    assert result["timed_out_after_output"] is True


def test_officecli_runner_accepts_plain_path_for_completed_png(tmp_path: Path) -> None:
    output = tmp_path / "preview.png"
    parent_code = (
        "import pathlib,sys; "
        "pathlib.Path(sys.argv[1]).write_bytes(b'\\x89PNG\\r\\n\\x1a\\npreview'); "
        "print(sys.argv[1])"
    )
    runner = _OfficeCliRunner(OfficeCliInfo(binary=sys.executable, version="test"))
    try:
        result = runner.run(
            ["-c", parent_code, str(output)],
            completed_output_path=output,
        )
    finally:
        runner.close()

    assert result["exit_code"] == 0
    assert result["stdout"]["success"] is True
    assert result["unstructured_output"] is True

"""Pinned OfficeCLI bootstrapper and console-script launcher.

The Python package installs a lightweight ``officecli`` command.  On first use the
launcher downloads the platform asset declared by the OfficeCLI provider contract,
verifies its SHA-256 digest, caches it under the nanobot data directory, and then
executes it with upstream auto-updates disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from nanobot.config.paths import get_runtime_subdir

OFFICECLI_CONTRACT_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "officecli"
    / "references"
    / "officecli-runtime.json"
)
OFFICECLI_DIR_ENV = "NANOBOT_OFFICECLI_DIR"
OFFICECLI_SKIP_UPDATE_ENV = "OFFICECLI_SKIP_UPDATE"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class OfficeCliBootstrapError(RuntimeError):
    """Raised when the pinned OfficeCLI runtime cannot be prepared safely."""


@dataclass(frozen=True)
class OfficeCliAsset:
    """One platform-specific asset selected from the provider contract."""

    platform_key: str
    name: str
    sha256: str
    version: str
    url: str


def load_officecli_contract(path: Path | None = None) -> dict[str, Any]:
    """Load the packaged OfficeCLI provider contract."""
    contract_path = path or OFFICECLI_CONTRACT_PATH
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfficeCliBootstrapError(
            f"Cannot read OfficeCLI runtime contract: {contract_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise OfficeCliBootstrapError("OfficeCLI runtime contract must be a JSON object")
    return payload


def _machine_key(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"x86_64", "amd64", "x64"}:
        return "x64"
    raise OfficeCliBootstrapError(f"Unsupported OfficeCLI CPU architecture: {value}")


def _platform_key(
    *,
    system: str | None = None,
    machine: str | None = None,
    alpine: bool | None = None,
) -> str:
    system_name = (system or platform.system()).strip().lower()
    architecture = _machine_key(machine or platform.machine())
    if system_name == "darwin":
        return f"mac-{architecture}"
    if system_name == "windows":
        return f"win-{architecture}"
    if system_name == "linux":
        is_alpine = Path("/etc/alpine-release").is_file() if alpine is None else alpine
        prefix = "linux-alpine" if is_alpine else "linux"
        return f"{prefix}-{architecture}"
    raise OfficeCliBootstrapError(f"Unsupported OfficeCLI operating system: {system_name}")


def select_officecli_asset(
    contract: dict[str, Any],
    *,
    system: str | None = None,
    machine: str | None = None,
    alpine: bool | None = None,
) -> OfficeCliAsset:
    """Select and validate the exact release asset for the current platform."""
    platform_key = _platform_key(system=system, machine=machine, alpine=alpine)
    assets = contract.get("assets")
    asset = assets.get(platform_key) if isinstance(assets, dict) else None
    if not isinstance(asset, dict):
        raise OfficeCliBootstrapError(
            f"OfficeCLI contract has no asset for platform {platform_key!r}"
        )

    version = str(contract.get("validated_version", "")).strip()
    release_url = str(contract.get("release_url", "")).strip()
    name = str(asset.get("name", "")).strip()
    digest = str(asset.get("sha256", "")).strip().lower()
    if not version or not name or len(digest) != 64:
        raise OfficeCliBootstrapError(
            f"OfficeCLI contract asset {platform_key!r} is incomplete"
        )
    marker = "/releases/tag/"
    if not release_url.startswith("https://") or marker not in release_url:
        raise OfficeCliBootstrapError("OfficeCLI release_url must be an HTTPS release tag URL")
    base_url = release_url.replace(marker, "/releases/download/", 1).rstrip("/")
    return OfficeCliAsset(
        platform_key=platform_key,
        name=name,
        sha256=digest,
        version=version,
        url=f"{base_url}/{name}",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_asset(url: str, destination: Path) -> None:
    """Download one release asset while emitting bounded progress to stderr."""
    timeout = httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0)
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            try:
                total = int(response.headers.get("content-length") or 0)
            except ValueError:
                total = 0
            received = 0
            next_report = 10
            last_report = time.monotonic()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                    handle.write(chunk)
                    received += len(chunk)
                    if total > 0:
                        percent = min(100, received * 100 // total)
                        if percent >= next_report:
                            print(
                                f"[officecli] download {percent}% "
                                f"({received / 1024 / 1024:.1f} MiB)",
                                file=sys.stderr,
                                flush=True,
                            )
                            next_report = ((percent // 10) + 1) * 10
                    elif time.monotonic() - last_report >= 10:
                        print(
                            f"[officecli] downloaded {received / 1024 / 1024:.1f} MiB",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_report = time.monotonic()


def get_officecli_runtime_dir() -> Path:
    """Return the stable cache root used by the pinned launcher."""
    override = os.environ.get(OFFICECLI_DIR_ENV)
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return get_runtime_subdir("officecli")


def _ensure_executable(path: Path) -> None:
    if os.name != "nt":
        path.chmod(
            path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )


def ensure_officecli(
    *,
    contract: dict[str, Any] | None = None,
    runtime_dir: Path | None = None,
    downloader: Callable[[str, Path], None] | None = None,
    system: str | None = None,
    machine: str | None = None,
    alpine: bool | None = None,
) -> Path:
    """Return a verified pinned OfficeCLI binary, downloading it when necessary."""
    selected = select_officecli_asset(
        contract or load_officecli_contract(),
        system=system,
        machine=machine,
        alpine=alpine,
    )
    root = runtime_dir or get_officecli_runtime_dir()
    install_dir = root / selected.version / selected.platform_key
    install_dir.mkdir(parents=True, exist_ok=True)
    destination = install_dir / selected.name
    lock = FileLock(str(install_dir / ".install.lock"), timeout=600)

    with lock:
        if (
            destination.is_file()
            and not destination.is_symlink()
            and _sha256(destination) == selected.sha256
        ):
            _ensure_executable(destination)
            return destination

        print(
            f"[officecli] preparing pinned OfficeCLI {selected.version} "
            f"for {selected.platform_key}",
            file=sys.stderr,
            flush=True,
        )
        partial = install_dir / f".{selected.name}.{os.getpid()}.part"
        partial.unlink(missing_ok=True)
        try:
            (downloader or _download_asset)(selected.url, partial)
            actual_digest = _sha256(partial)
            if actual_digest != selected.sha256:
                raise OfficeCliBootstrapError(
                    "OfficeCLI checksum mismatch: "
                    f"expected {selected.sha256}, got {actual_digest}"
                )
            _ensure_executable(partial)
            os.replace(partial, destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        print(
            f"[officecli] ready: {destination}",
            file=sys.stderr,
            flush=True,
        )
        return destination


def _execution_env() -> dict[str, str]:
    env = dict(os.environ)
    env[OFFICECLI_SKIP_UPDATE_ENV] = "1"
    return env


def main() -> None:
    """Prepare and execute the pinned OfficeCLI binary."""
    try:
        binary = ensure_officecli()
    except (OfficeCliBootstrapError, FileLockTimeout, OSError, httpx.HTTPError) as exc:
        print(f"officecli bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    argv = [str(binary), *sys.argv[1:]]
    env = _execution_env()
    if os.name == "nt":
        raise SystemExit(subprocess.call(argv, env=env))
    os.execve(binary, argv, env)


if __name__ == "__main__":
    main()

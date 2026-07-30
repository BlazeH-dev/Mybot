"""Local credential resolution for evaluation-only external tools."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping

ADOBE_CLIENT_ID = "PDF_SERVICES_CLIENT_ID"
ADOBE_CLIENT_SECRET = "PDF_SERVICES_CLIENT_SECRET"
ADOBE_KEYCHAIN_SERVICE = "Mybot Adobe PDF Services"


def _security(*arguments: str) -> subprocess.CompletedProcess[str] | None:
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.run(
            ["/usr/bin/security", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _keychain_password(service: str, *, account: str | None = None) -> str | None:
    arguments = ["find-generic-password", "-s", service]
    if account:
        arguments.extend(["-a", account])
    result = _security(*arguments, "-w")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _keychain_account(service: str) -> str | None:
    result = _security("find-generic-password", "-s", service)
    if result is None or result.returncode != 0:
        return None
    match = re.search(r'^\s*"acct"<blob>="([^"]+)"$', result.stdout, re.MULTILINE)
    return match.group(1) if match else None


def adobe_pdf_services_credentials(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve Adobe credentials without persisting or logging their values."""
    source = os.environ if environ is None else environ
    client_id = source.get(ADOBE_CLIENT_ID, "").strip()
    client_secret = source.get(ADOBE_CLIENT_SECRET, "").strip()

    if not client_id:
        client_id = (
            _keychain_password(ADOBE_CLIENT_ID)
            or _keychain_password(ADOBE_KEYCHAIN_SERVICE, account=ADOBE_CLIENT_ID)
            or ""
        )
    if not client_secret:
        client_secret = (
            _keychain_password(ADOBE_CLIENT_SECRET)
            or _keychain_password(ADOBE_KEYCHAIN_SERVICE, account=ADOBE_CLIENT_SECRET)
            or ""
        )
    if not client_id and client_secret:
        # Compatibility with one generic-password item: account is Client ID,
        # password is Client Secret, and service is PDF_SERVICES_CLIENT_SECRET.
        client_id = _keychain_account(ADOBE_CLIENT_SECRET) or ""

    return {
        name: value
        for name, value in (
            (ADOBE_CLIENT_ID, client_id),
            (ADOBE_CLIENT_SECRET, client_secret),
        )
        if value
    }


def adobe_pdf_services_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env.update(adobe_pdf_services_credentials(env))
    return env


def adobe_pdf_services_available(environ: Mapping[str, str] | None = None) -> bool:
    credentials = adobe_pdf_services_credentials(environ)
    return ADOBE_CLIENT_ID in credentials and ADOBE_CLIENT_SECRET in credentials

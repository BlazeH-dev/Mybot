from __future__ import annotations

from types import SimpleNamespace

from nanobot.evaluations.credentials import (
    ADOBE_CLIENT_ID,
    ADOBE_CLIENT_SECRET,
    ADOBE_KEYCHAIN_SERVICE,
    adobe_pdf_services_credentials,
)


def test_adobe_environment_credentials_take_precedence(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "nanobot.evaluations.credentials._security",
        lambda *arguments: calls.append(arguments),
    )

    credentials = adobe_pdf_services_credentials({
        ADOBE_CLIENT_ID: "environment-id",
        ADOBE_CLIENT_SECRET: "environment-secret",
    })

    assert credentials == {
        ADOBE_CLIENT_ID: "environment-id",
        ADOBE_CLIENT_SECRET: "environment-secret",
    }
    assert calls == []


def test_adobe_credentials_load_from_separate_keychain_services(monkeypatch) -> None:
    def security(*arguments: str):
        service = arguments[arguments.index("-s") + 1]
        values = {ADOBE_CLIENT_ID: "keychain-id", ADOBE_CLIENT_SECRET: "keychain-secret"}
        return SimpleNamespace(returncode=0, stdout=values[service] + "\n")

    monkeypatch.setattr("nanobot.evaluations.credentials._security", security)

    assert adobe_pdf_services_credentials({}) == {
        ADOBE_CLIENT_ID: "keychain-id",
        ADOBE_CLIENT_SECRET: "keychain-secret",
    }


def test_adobe_credentials_load_from_unified_keychain_service(monkeypatch) -> None:
    def security(*arguments: str):
        service = arguments[arguments.index("-s") + 1]
        account = arguments[arguments.index("-a") + 1] if "-a" in arguments else None
        if service != ADOBE_KEYCHAIN_SERVICE or not account:
            return SimpleNamespace(returncode=44, stdout="")
        values = {ADOBE_CLIENT_ID: "unified-id", ADOBE_CLIENT_SECRET: "unified-secret"}
        return SimpleNamespace(returncode=0, stdout=values[account] + "\n")

    monkeypatch.setattr("nanobot.evaluations.credentials._security", security)

    assert adobe_pdf_services_credentials({}) == {
        ADOBE_CLIENT_ID: "unified-id",
        ADOBE_CLIENT_SECRET: "unified-secret",
    }


def test_adobe_credentials_load_from_paired_secret_item(monkeypatch) -> None:
    def security(*arguments: str):
        service = arguments[arguments.index("-s") + 1]
        if service != ADOBE_CLIENT_SECRET:
            return SimpleNamespace(returncode=44, stdout="")
        if "-w" in arguments:
            return SimpleNamespace(returncode=0, stdout="paired-secret\n")
        return SimpleNamespace(returncode=0, stdout='    "acct"<blob>="paired-id"\n')

    monkeypatch.setattr("nanobot.evaluations.credentials._security", security)

    assert adobe_pdf_services_credentials({}) == {
        ADOBE_CLIENT_ID: "paired-id",
        ADOBE_CLIENT_SECRET: "paired-secret",
    }

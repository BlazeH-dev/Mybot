"""Normalization and binding helpers for minimal command network grants."""

from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import re
import shlex
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.azure.internal",
})


@dataclass(frozen=True, slots=True)
class NetworkGrant:
    domains: tuple[str, ...]
    ports: tuple[int, ...]
    command_hash: str
    expires_at: str
    addresses: tuple[str, ...] = ()


_CURRENT_NETWORK_GRANT: contextvars.ContextVar[NetworkGrant | None] = contextvars.ContextVar(
    "mybot_network_grant",
    default=None,
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_ANY_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_CURL_TARGET_OVERRIDE_FLAGS = frozenset({
    "--abstract-unix-socket",
    "--alt-svc",
    "--config",
    "--connect-to",
    "--dns-interface",
    "--dns-ipv4-addr",
    "--dns-ipv6-addr",
    "--dns-servers",
    "--hsts",
    "--interface",
    "--location",
    "--location-trusted",
    "--preproxy",
    "--proxy",
    "--proxy-header",
    "--proxy-user",
    "--resolve",
    "--socks4",
    "--socks4a",
    "--socks5",
    "--socks5-hostname",
    "--unix-socket",
})


def bind_network_grant(grant: NetworkGrant):
    return _CURRENT_NETWORK_GRANT.set(grant)


def reset_network_grant(token) -> None:
    _CURRENT_NETWORK_GRANT.reset(token)


def current_network_grant() -> NetworkGrant | None:
    return _CURRENT_NETWORK_GRANT.get()


def network_grant_active(grant: NetworkGrant) -> bool:
    try:
        expires = datetime.fromisoformat(grant.expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < expires.astimezone(timezone.utc)


def command_network_targets(command: str) -> tuple[tuple[str, ...], tuple[int, ...], bool]:
    """Extract public HTTP targets and whether the command is a single safe fetch argv."""
    urls = _URL_RE.findall(command)
    domains: set[str] = set()
    ports: set[int] = set()
    for url in urls:
        parsed = urlparse(url)
        domains.add(normalize_domain(url))
        ports.add(parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    try:
        argv = shlex.split(command)
    except ValueError:
        return tuple(sorted(domains)), tuple(sorted(ports)), False
    simple = bool(argv) and Path(argv[0]).name == "curl"
    if any(token in command for token in (";", "&&", "||", "|", "`", "$(", ">", "<")):
        simple = False
    for token in argv[1:]:
        option = token.split("=", 1)[0]
        if option in _CURL_TARGET_OVERRIDE_FLAGS or token == "-L" or token.startswith(("-K", "-x")):
            simple = False
        if _ANY_SCHEME_RE.match(token) and not token.lower().startswith(("http://", "https://")):
            simple = False
    return tuple(sorted(domains)), tuple(sorted(ports)), simple and bool(domains)


def encode_address_binding(domain: str, address: str) -> str:
    normalized = normalize_domain(domain)
    parsed = ipaddress.ip_address(address)
    if not parsed.is_global:
        raise ValueError(f"blocked private/internal network address: {address}")
    return f"{normalized}={parsed.compressed}"


def pinned_curl_argv(command: str, grant: NetworkGrant) -> tuple[str, ...]:
    """Build a curl argv pinned to the exact approved DNS result and target set."""
    domains, ports, minimal = command_network_targets(command)
    if not minimal or domains != grant.domains or ports != grant.ports:
        raise ValueError("network command no longer matches its approved domain/port binding")
    argv = shlex.split(command)
    if not argv or Path(argv[0]).name != "curl":
        raise ValueError("approved restricted networking only supports a direct curl argv")
    address_map: dict[str, list[str]] = {}
    for raw in grant.addresses:
        domain, separator, address = raw.partition("=")
        if not separator:
            raise ValueError("invalid approved DNS address binding")
        normalized = normalize_domain(domain)
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global:
            raise ValueError(f"approved DNS address is no longer public: {address}")
        address_map.setdefault(normalized, []).append(parsed.compressed)

    pins: set[tuple[str, int, str]] = set()
    for url in _URL_RE.findall(command):
        parsed = urlparse(url)
        domain = normalize_domain(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = address_map.get(domain)
        if not addresses:
            raise ValueError(f"approved DNS binding is missing for domain: {domain}")
        pins.add((domain, port, sorted(addresses)[0]))

    pinned = [argv[0], "-q", *argv[1:]]
    for domain, port, address in sorted(pins):
        curl_address = f"[{address}]" if ":" in address else address
        pinned.extend(("--resolve", f"{domain}:{port}:{curl_address}"))
    pinned.extend(("--proto", "=http,https", "--proto-redir", "=http,https", "--max-redirs", "0"))
    return tuple(pinned)


def command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def normalize_domain(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("empty network target")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(f"invalid network target: {value!r}")
    if host in _BLOCKED_HOSTS:
        raise ValueError(f"blocked private/internal network target: {host}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError(f"blocked private/internal network target: {host}")
    return host.encode("idna").decode("ascii")


def normalize_port(value: int) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid network port: {value}")
    return port


def resolve_public_addresses(domain: str) -> tuple[str, ...]:
    """Resolve a domain and reject any private/rebinding destination."""
    normalized = normalize_domain(domain)
    addresses: set[str] = set()
    for item in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM):
        raw = item[4][0]
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ValueError(f"domain resolves to private/internal address: {normalized}")
        addresses.add(address.compressed)
    if not addresses:
        raise ValueError(f"domain did not resolve: {normalized}")
    return tuple(sorted(addresses))


def validate_redirect_target(url: str, grant: NetworkGrant) -> str:
    host = normalize_domain(url)
    if host not in grant.domains:
        raise ValueError(f"redirect target is outside approved domains: {host}")
    resolve_public_addresses(host)
    return host

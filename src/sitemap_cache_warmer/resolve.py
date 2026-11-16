"""``--resolve host:port:address`` support: connect to a chosen IP while keeping the real host name.

This is how you warm the cache on the origin server itself, bypassing the CDN or DNS: the request goes to
``address`` but keeps the original ``Host`` header and the TLS server name (SNI), so the web server picks the right
virtual host and certificate, and the certificate is still verified against the real host name.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ResolveRule:
    host: str
    port: int
    address: str

    @classmethod
    def parse(cls, text: str) -> ResolveRule:
        """Parse curl's ``host:port:address`` format; the address may be an IPv6 literal in brackets."""
        host, sep1, rest = text.partition(":")
        port_text, sep2, address = rest.partition(":")
        if not (sep1 and sep2 and host and port_text and address):
            raise ValueError(f"--resolve must look like host:port:address, got {text!r}")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"bad port in --resolve {text!r}") from exc
        if not 0 < port < 65536:
            raise ValueError(f"bad port in --resolve {text!r}")
        address = address.strip()
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]
        try:
            ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"--resolve address must be an IP address, got {address!r}") from exc
        return cls(host.lower(), port, address)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


class ResolvingTransport(httpx.AsyncBaseTransport):
    """Wraps another transport and rewrites the connection target for matching host and port pairs."""

    def __init__(self, inner: httpx.AsyncBaseTransport, rules: list[ResolveRule]) -> None:
        self.inner = inner
        self.rules = {(r.host, r.port): r.address for r in rules}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or _default_port(url.scheme)
        address = self.rules.get((url.host.lower(), port))
        if address is not None:
            original_host = url.host
            # The Host header was already set from the original URL by the client.
            request.url = url.copy_with(host=address)
            if url.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": original_host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()

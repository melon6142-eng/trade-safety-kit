"""Deny-by-default HTTP transport gate for broker APIs.

Every outgoing request is classified against a closed inventory of
(origin, path) pairs before it reaches the wire:

* ``read`` paths pass.
* ``exit`` paths (sell / close) pass.
* ``entry`` paths (new risk) pass **only** with an ``entry_claim`` that matches a
  live allowlist row (see :mod:`trade_safety_kit.allowlist`).
* Anything else - unknown origin, path, method, query string, redirect,
  credentials in the URL, header smuggling - raises :class:`TransportDenied`.

Reason codes never echo URLs, headers or secret values.
"""
from __future__ import annotations

import re
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from trade_safety_kit import allowlist


class TransportDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class TransportPolicy:
    origin: str
    read_paths: frozenset = field(default_factory=frozenset)
    entry_paths: frozenset = field(default_factory=frozenset)
    exit_paths: frozenset = field(default_factory=frozenset)
    allowed_methods: frozenset = frozenset({"GET", "POST"})
    allowlist_path: str | Path | None = None

    def classify(self, method, url, *, entry_claim=None) -> str:
        if (type(url) is not str or url != url.strip() or type(method) is not str
                or method not in self.allowed_methods
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise TransportDenied("transport_method_or_url_unknown")
        try:
            value = urllib.parse.urlsplit(url)
            origin = f"{value.scheme}://{value.netloc}"
        except ValueError:
            raise TransportDenied("transport_url_unknown") from None
        if origin != self.origin or value.fragment or value.username or value.password:
            raise TransportDenied("transport_origin_or_alias_unknown")
        if value.query:
            raise TransportDenied("transport_query_alias_forbidden")
        if value.path in self.entry_paths:
            if type(entry_claim) is not dict:
                raise TransportDenied("new_risk_denied_no_entry_claim")
            try:
                allowlist.match(entry_claim.get("symbol"), entry_claim.get("quantity"),
                                entry_claim.get("limit_price"), path=self.allowlist_path)
            except allowlist.AllowlistDenied as exc:
                raise TransportDenied(f"entry_allowlist_denied:{exc}") from None
            return "entry_allowlisted"
        if value.path in self.read_paths:
            return "read"
        if value.path in self.exit_paths:
            return "exit"
        raise TransportDenied("unclassified_operation_denied")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise TransportDenied("transport_redirect_denied")


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class GuardedOpener:
    """Wrap a urllib opener; re-validate and rebuild each request before sending."""

    def __init__(self, delegate, policy: TransportPolicy, claim=None):
        self._delegate, self._policy, self._claim = delegate, policy, claim

    def open(self, request, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        if type(request) is not urllib.request.Request:
            raise TransportDenied("typed_http_request_required")
        if data is not None:
            raise TransportDenied("transport_body_override_forbidden")
        url, method, body = request.full_url, urllib.request.Request.get_method(request), request.data
        self._policy.classify(method, url, entry_claim=self._claim)
        if body is not None and type(body) is not bytes:
            raise TransportDenied("transport_immutable_body_required")
        canonical = urllib.request.Request(url, data=body, method=method)
        if (any(type(getattr(request, f)) is not str or getattr(request, f) != getattr(canonical, f)
                for f in ("selector", "host", "type")) or request._tunnel_host is not None):
            raise TransportDenied("transport_wire_destination_conflict")
        headers = {}
        for source in (request.headers, request.unredirected_hdrs):
            for key, value in dict(source).items():
                if (type(key) is not str or not _HEADER_NAME.fullmatch(key) or type(value) is not str
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    raise TransportDenied("transport_header_shape_unknown")
                name = key.lower()
                if name in headers or name in {"host", "proxy-authorization", "transfer-encoding"}:
                    raise TransportDenied("transport_routing_or_duplicate_header_forbidden")
                if name == "content-length" and value != str(len(body or b"")):
                    raise TransportDenied("transport_body_length_conflict")
                headers[name] = value
        frozen = urllib.request.Request(url, data=body, headers=headers, method=method)
        return self._delegate.open(frozen, timeout=timeout)

"""HTTP transport hardening for distributor sync.

Two concerns:

* Distributor URLs must be ``https://`` or loopback ``http://`` (127.0.0.1,
  ::1, localhost). Any other scheme — especially ``file://`` — is rejected
  before a request is made. Loopback http is allowed so local mock servers
  and dev distributors can be wired in without disabling the guard.
* Redirects must satisfy the same rule. ``urllib.request.HTTPRedirectHandler``
  will otherwise happily follow a 30x to ``file://`` or remote ``http://``,
  which would let a hostile distributor (or a MITM upstream of HTTP) read
  local files or downgrade the channel.
"""
from __future__ import annotations

from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, build_opener

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class HttpsOnlyError(ValueError):
    """Raised when an unsupported URL is passed to a distributor HTTP call."""


def _url_is_allowed(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme
    if scheme == "https":
        return True, ""
    if scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS:
        return True, ""
    return False, (
        f"only https:// (or http:// to a loopback host) URLs are allowed, "
        f"got '{scheme}://{parsed.hostname or ''}': {url}"
    )


class _SecureRedirectHandler(HTTPRedirectHandler):
    """Reject any redirect target that fails ``_url_is_allowed``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        ok, reason = _url_is_allowed(newurl)
        if not ok:
            raise URLError(f"refusing {code} redirect: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_https(url: str) -> None:
    """Raise ``HttpsOnlyError`` unless ``url`` is https or loopback http."""
    ok, reason = _url_is_allowed(url)
    if not ok:
        raise HttpsOnlyError(reason)


def secure_opener() -> OpenerDirector:
    """Return an opener that rejects redirects to non-allowed targets."""
    return build_opener(_SecureRedirectHandler())

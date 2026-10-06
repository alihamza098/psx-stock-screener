#!/usr/bin/env python3
"""
One HTTP client for everything the app fetches from PSX (dps.psx.com.pk).

* TLS: certificates are verified. For PSX hosts only, a verification failure falls back to an
  unverified connection once, with a logged warning (PSX_STRICT_TLS=1 refuses).
* Request token: since 2026-09-24 PSX answers data requests with 403 unless they carry an
  ``X-Req-Id`` header equal to ``window.__ps._k`` — a token PSX embeds in every HTML page.
  The token is read from the home page, cached ~4 minutes, refreshed after a 403, and sent on
  every DPS request. If no token can be obtained, requests go out without it (fail open), so
  nothing breaks if PSX removes the mechanism.
* POST support (``data=``) for endpoints such as /historical, gzip/deflate decoding, retries.

Pure stdlib.
"""

import gzip
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, Optional

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()

INSECURE_TLS_FALLBACK_HOSTS = {"dps.psx.com.pk", "www.psx.com.pk", "psx.com.pk"}
_INSECURE_SSL_CONTEXT = ssl.create_default_context()
_INSECURE_SSL_CONTEXT.check_hostname = False
_INSECURE_SSL_CONTEXT.verify_mode = ssl.CERT_NONE
TLS_STATUS: Dict[str, Any] = {"insecure_fallback_used": False, "last_error": None}

DPS_HOST = "dps.psx.com.pk"
TOKEN_PAGE = "https://dps.psx.com.pk/"
TOKEN_HEADER = "X-Req-Id"
TOKEN_TTL = 240          # seconds; PSX rotates roughly every 5 minutes
TOKEN_RETRY_BACKOFF = 60  # seconds before retrying after a failed token fetch

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

_PS_SCRIPT_RE = re.compile(r"window\.__ps\s*=\s*(\{.*?\})\s*(?:;|</script>)", re.DOTALL)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")

_token_lock = threading.Lock()
_token: Dict[str, Any] = {"value": None, "at": 0.0, "failed_at": 0.0}
TOKEN_STATUS: Dict[str, Any] = {"last_error": None, "fetched_at": None}


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def _tls_context_for(url: str, verified_failed: bool = False) -> ssl.SSLContext:
    if verified_failed and _host(url) in INSECURE_TLS_FALLBACK_HOSTS and os.environ.get("PSX_STRICT_TLS") != "1":
        return _INSECURE_SSL_CONTEXT
    return SSL_CONTEXT


def extract_token(html: str) -> Optional[str]:
    """``window.__ps._k`` from a PSX HTML page, or None."""
    m = _PS_SCRIPT_RE.search(html or "")
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return None
    tok = payload.get("_k") if isinstance(payload, dict) else None
    return tok if isinstance(tok, str) and _TOKEN_RE.fullmatch(tok) else None


def _decode(raw: bytes, encoding: str) -> str:
    enc = (encoding or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw)
        except Exception:
            pass
    return raw.decode("utf-8", errors="ignore")


def _open(url: str, headers: Dict[str, str], data: Optional[bytes], timeout: float) -> str:
    """One request with the TLS-verify-then-PSX-fallback rule. Raises on HTTP/network errors."""
    req = urllib.request.Request(url, headers=headers, data=data, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_context_for(url)) as r:
            return _decode(r.read(), r.info().get("Content-Encoding", ""))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if not isinstance(reason, ssl.SSLCertVerificationError):
            raise
        ctx = _tls_context_for(url, verified_failed=True)
        if ctx is not _INSECURE_SSL_CONTEXT:
            raise
        if not TLS_STATUS["insecure_fallback_used"]:
            print(f"[PSX] WARNING: certificate verification failed for {url} ({reason}); "
                  f"falling back to an unverified connection for PSX hosts. Set PSX_STRICT_TLS=1 to refuse.")
        TLS_STATUS.update(insecure_fallback_used=True, last_error=str(reason))
        req = urllib.request.Request(url, headers=headers, data=data, method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return _decode(r.read(), r.info().get("Content-Encoding", ""))


def get_token(force: bool = False) -> Optional[str]:
    """Current PSX request token (cached); None if it cannot be obtained."""
    now = time.time()
    with _token_lock:
        if not force and _token["value"] and now - _token["at"] < TOKEN_TTL:
            return _token["value"]
        if not force and _token["failed_at"] and now - _token["failed_at"] < TOKEN_RETRY_BACKOFF:
            return None
        try:
            html = _open(TOKEN_PAGE, dict(BROWSER_HEADERS), None, 20)
            tok = extract_token(html)
        except Exception as e:
            tok = None
            TOKEN_STATUS["last_error"] = str(e)
        if tok:
            _token.update(value=tok, at=now, failed_at=0.0)
            TOKEN_STATUS.update(last_error=None, fetched_at=now)
        else:
            _token.update(value=None, at=0.0, failed_at=now)
            TOKEN_STATUS["last_error"] = TOKEN_STATUS["last_error"] or "window.__ps._k not found on PSX home page"
        return tok


def invalidate_token(tok: Optional[str]) -> None:
    with _token_lock:
        if tok and _token["value"] == tok:
            _token.update(value=None, at=0.0)


def fetch(url: str, timeout: float = 25, retries: int = 3, data: Optional[Dict[str, Any]] = None,
          headers: Optional[Dict[str, str]] = None) -> str:
    """GET (or POST when ``data`` is given) with retries; PSX token handling for dps.psx.com.pk."""
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    is_dps = _host(url) == DPS_HOST
    base_headers = dict(BROWSER_HEADERS)
    if is_dps:
        base_headers.update({"Referer": "https://dps.psx.com.pk/", "X-Requested-With": "XMLHttpRequest"})
    if body is not None:
        base_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    base_headers.update(headers or {})

    last_error: Optional[Exception] = None
    auth_retry_used = False
    attempt = 0
    while attempt < retries:
        attempt += 1
        tok = get_token() if is_dps else None
        h = dict(base_headers)
        if tok:
            h[TOKEN_HEADER] = tok
        try:
            return _open(url, h, body, timeout)
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 403 and is_dps and not auth_retry_used:
                auth_retry_used = True
                invalidate_token(tok)
                if get_token(force=True):
                    attempt -= 1  # the token refresh retry is not counted as an attempt
                    continue
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last_error = e
        print(f"[PSX] Fetch error (attempt {attempt}/{retries}) for {url}: {last_error}")
        if attempt < retries:
            time.sleep(1.0 * attempt)
    raise last_error if last_error else RuntimeError(f"fetch failed: {url}")

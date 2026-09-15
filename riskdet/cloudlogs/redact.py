"""Secret redaction for Cloud Logging evidence -- runs INSIDE the writer.

The acp-prod spin-server logs full request payloads, and the `token` field is
a LIVE JWT credential. Anything written to disk goes through redact() first;
there is no code path that serialises an un-redacted entry, and the writer
re-scans every byte it produced before closing the file (belt and braces).

Redaction is correlation-preserving: each secret is replaced by
`REDACTED:<kind>:<salted-sha256-prefix>` so two log entries carrying the SAME
token remain linkable while the token itself is unrecoverable. The salt is
per-run (config RISKDET_REDACT_SALT, random when unset) so digests cannot be
rainbow-tabled across runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from ..errors import RedactionError

# eyJ... = base64 of '{"'. A full JWT is three dot-separated segments; but a
# TRUNCATED jwt or ANY base64-encoded JSON blob shares the same prefix and is
# indistinguishable from a credential without decoding. Found live: spin-server
# logs a base64 `userStatus` game-state blob that a JWT-only regex passes.
# Policy at this boundary: over-redaction beats leak -- every eyJ base64 run
# is redacted, full JWTs first (for kind labelling), then any remainder.
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
# no minimum length: assert_clean() is a bare-'eyJ' gate, so redaction must
# catch every run the gate would refuse -- otherwise a stray short 'eyJ..'
# blocks a whole evidence file (fail-closed, but needlessly)
B64_BLOB_RE = re.compile(r"\beyJ[A-Za-z0-9_+/=-]*(?:\.[A-Za-z0-9_+/=-]+)*")
BEARER_RE = re.compile(r"(?i)\b(bearer|authorization)\s*[:=]\s*\S+")

# any field whose NAME suggests a credential is redacted wholesale,
# independent of what the value looks like
TOKEN_FIELD_KEYS = frozenset({
    "token", "access_token", "id_token", "refresh_token", "authorization",
    "auth", "jwt", "session", "cookie", "set-cookie", "apikey", "api_key",
    "password", "secret", "signature", "sign", "private_key",
})


@dataclass
class RedactionResult:
    payload: Any
    redactions: int = 0
    kinds: dict[str, int] = field(default_factory=dict)


class Redactor:
    def __init__(self, salt: str | None = None) -> None:
        self._salt = salt or secrets.token_hex(16)

    def _tag(self, kind: str, secret: str) -> str:
        digest = hashlib.sha256(
            (self._salt + secret).encode("utf-8")).hexdigest()[:12]
        return f"REDACTED:{kind}:sha256={digest}"

    def redact(self, obj: Any) -> RedactionResult:
        res = RedactionResult(payload=None)
        res.payload = self._walk(obj, res)
        return res

    # ------------------------------------------------------------------ #
    def _walk(self, obj: Any, res: RedactionResult) -> Any:
        if isinstance(obj, str):
            return self._string(obj, res)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                key_l = str(k).lower()
                if key_l in TOKEN_FIELD_KEYS and isinstance(v, str) and v:
                    out[k] = self._tag("token_field", v)
                    res.redactions += 1
                    res.kinds["token_field"] = res.kinds.get("token_field", 0) + 1
                else:
                    out[k] = self._walk(v, res)
            return out
        if isinstance(obj, (list, tuple)):
            return [self._walk(v, res) for v in obj]
        return obj

    def _string(self, s: str, res: RedactionResult) -> str:
        def sub_jwt(m: re.Match) -> str:
            res.redactions += 1
            res.kinds["jwt"] = res.kinds.get("jwt", 0) + 1
            return self._tag("jwt", m.group(0))

        def sub_bearer(m: re.Match) -> str:
            res.redactions += 1
            res.kinds["bearer"] = res.kinds.get("bearer", 0) + 1
            return self._tag("bearer", m.group(0))

        def sub_blob(m: re.Match) -> str:
            res.redactions += 1
            res.kinds["b64_blob"] = res.kinds.get("b64_blob", 0) + 1
            return self._tag("b64_blob", m.group(0))

        s = JWT_RE.sub(sub_jwt, s)
        s = BEARER_RE.sub(sub_bearer, s)
        s = B64_BLOB_RE.sub(sub_blob, s)   # anything eyJ-shaped that remains
        return s


def assert_clean(serialized: str, *, where: str) -> None:
    """Final scan before a file closes: the BARE 'eyJ' prefix anywhere is
    FATAL. Strictest possible check -- a truncated JWT and a base64 JSON blob
    are indistinguishable from a credential without decoding, so none of them
    may reach disk."""
    if "eyJ" in serialized:
        raise RedactionError(
            f"{where}: an eyJ-prefixed base64 run survived redaction -- "
            f"refusing to write. This is a bug in redact.py; fix it before "
            f"pulling evidence.")


def dumps_redacted(obj: Any, redactor: Redactor, *, where: str,
                   **json_kwargs: Any) -> tuple[str, RedactionResult]:
    """The ONLY serialisation path the evidence writer uses."""
    res = redactor.redact(obj)
    text = json.dumps(res.payload, ensure_ascii=False, default=str,
                      **json_kwargs)
    assert_clean(text, where=where)
    return text, res

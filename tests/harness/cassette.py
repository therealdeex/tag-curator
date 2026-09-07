"""Cassette-based GraphQL record/replay layer for the mocked-Stash test harness.

A *cassette* is a JSON description of one or more recorded GraphQL interactions.
The on-disk shape (decision D7 of the stash-tag-curator plan) is::

    {
      "request":  {"query": "...", "variables": {...}},
      "response": {"data": {...}, "errors": [...]}
    }

This module supports three equivalent file shapes so a single file may carry a
single interaction, an ordered list of interactions (for multi-step replays such
as a ``429`` immediately followed by a ``200``), or an explicit envelope::

    # single
    {"request": {...}, "response": {...}}

    # ordered list
    [{"request": {...}, "response": {...}}, {...}]

    # explicit envelope (lets a file carry a name + metadata)
    {"name": "rate-limited-429", "interactions": [{...}, {...}]}

Each interaction may carry the following optional keys:

* ``request.variables``    -- recorded variables; used for matching when
                              ``request.match_variables`` is true.
* ``request.match_variables`` -- when true the interaction only matches a call
                              whose submitted variables are a superset of the
                              recorded ones (default false, match on operation
                              signature only).
* ``request.signature``    -- override the derived operation signature.
* ``response.http_status`` -- HTTP status code the HTTP mock should emit
                              (default ``200``). Lets cassettes model ``429``.
* ``response.headers``     -- extra response headers (e.g. ``Retry-After``).
* ``response.data``        -- GraphQL ``data`` payload (may be ``null``).
* ``response.errors``      -- GraphQL ``errors`` array (may be absent).
* ``reusable``             -- when true the interaction is never consumed by
                              FIFO replay (useful for idempotent lookups such
                              as ``GetConfiguration`` that many tests hit).

The module is deliberately free of any dependency on a live Stash, ``graphql``,
or ``requests``.  Recording mode (``CassetteRecorder``) uses ``urllib`` from the
standard library so it can run on a host without third-party packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Interaction",
    "Cassette",
    "CassetteLibrary",
    "CassetteRecorder",
    "CassetteError",
    "CassetteNotFoundError",
    "signature_for_query",
    "load_interaction",
    "load_interactions",
]

# ---------------------------------------------------------------------------
# Signature derivation
# ---------------------------------------------------------------------------

# Matches the leading ``query``/``mutation`` ``OperationName`` token.  GraphQL
# allows leading whitespace, comments and newlines before the operation; we
# strip line comments first so unnamed inline fragments are still handled.
_OP_RE = re.compile(
    r"""(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)""",
    re.VERBOSE,
)


def signature_for_query(query: str) -> str:
    """Return a stable signature for a GraphQL operation string.

    The signature is the parsed operation name when one is present, otherwise a
    short SHA-1 of the stripped query text.  Two queries with the same operation
    name therefore share a signature (the common case for cassette replay);
    anonymous queries fall back to a content hash so they still replay
    deterministically.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    stripped = "\n".join(
        line for line in query.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    if not stripped:
        return "empty"
    m = _OP_RE.match(stripped)
    if m:
        return m.group(1)
    return "sha1_" + hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CassetteError(Exception):
    """Base class for cassette-layer failures."""


class CassetteNotFoundError(CassetteError):
    """Raised when no recorded interaction matches a submitted call."""


# ---------------------------------------------------------------------------
# Interaction model
# ---------------------------------------------------------------------------


@dataclass
class Interaction:
    """One recorded GraphQL request/response pair.

    ``consumed`` is mutable: ordered FIFO replay pops matched non-reusable
    interactions so a cassette can encode sequences (``429`` → ``200``).
    """

    request_query: str
    response_data: Any = None
    response_errors: list | None = None
    variables: Mapping[str, Any] = field(default_factory=dict)
    match_variables: bool = False
    signature_override: str | None = None
    http_status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    reusable: bool = False
    consumed: bool = False
    # Recorded name (for diagnostics); set by the loader when present.
    label: str | None = None

    # -- signature --------------------------------------------------------

    @property
    def signature(self) -> str:
        if self.signature_override:
            return self.signature_override
        return signature_for_query(self.request_query)

    # -- matching ---------------------------------------------------------

    def matches(self, query: str, variables: Mapping[str, Any] | None) -> bool:
        """Return True if this interaction can serve the given call.

        Two interactions with the same signature are distinguished by FIFO
        ordering (``Cassette.replay`` consumes the first available match); when
        ``match_variables`` is set the submitted variables must contain every
        recorded key with an equal value.
        """
        if self.signature != signature_for_query(query):
            return False
        if self.match_variables:
            submitted = variables or {}
            for key, expected in (self.variables or {}).items():
                if key not in submitted:
                    return False
                if submitted[key] != expected:
                    return False
        return True

    # -- serialisation ----------------------------------------------------

    @property
    def response(self) -> dict:
        """The ``{data, errors}`` payload a Stash client would receive."""
        out: dict[str, Any] = {"data": self.response_data}
        if self.response_errors is not None:
            out["errors"] = self.response_errors
        return out

    def to_dict(self) -> dict:
        """Serialise to the canonical on-disk shape."""
        request: dict[str, Any] = {"query": self.request_query}
        if self.variables:
            request["variables"] = dict(self.variables)
        if self.match_variables:
            request["match_variables"] = True
        if self.signature_override:
            request["signature"] = self.signature_override
        response: dict[str, Any] = dict(self.response)
        if self.http_status != 200:
            response["http_status"] = self.http_status
        if self.headers:
            response["headers"] = dict(self.headers)
        out: dict[str, Any] = {"request": request, "response": response}
        if self.reusable:
            out["reusable"] = True
        if self.label:
            out["label"] = self.label
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_interaction(raw: Mapping[str, Any]) -> Interaction:
    """Build an :class:`Interaction` from one decoded JSON object."""
    if "request" not in raw or "response" not in raw:
        raise CassetteError(
            "interaction must have 'request' and 'response' keys; got: "
            + json.dumps(raw)[:200]
        )
    request = raw["request"]
    response = raw["response"]
    if not isinstance(request, Mapping) or "query" not in request:
        raise CassetteError("interaction.request must be a mapping with 'query'")
    interaction = Interaction(
        request_query=request["query"],
        variables=dict(request.get("variables") or {}),
        match_variables=bool(request.get("match_variables", False)),
        signature_override=request.get("signature"),
        response_data=response.get("data"),
        response_errors=response.get("errors"),
        http_status=int(response.get("http_status", 200)),
        headers=dict(response.get("headers") or {}),
        reusable=bool(raw.get("reusable", False)),
        label=raw.get("label"),
    )
    return interaction


def load_interactions(raw: Any) -> list[Interaction]:
    """Decode any supported cassette JSON shape into an ordered list."""
    if isinstance(raw, Mapping):
        if "interactions" in raw:
            items = raw["interactions"]
            if not isinstance(items, Iterable):
                raise CassetteError("'interactions' must be a list")
            return [load_interaction(i) for i in items]
        if "request" in raw and "response" in raw:
            return [load_interaction(raw)]
        raise CassetteError(
            "unsupported cassette object shape; keys=" + str(sorted(raw.keys()))
        )
    if isinstance(raw, list):
        if not raw:
            return []
        return [load_interaction(i) for i in raw]
    raise CassetteError("cassette JSON must be object, list, or envelope")


# ---------------------------------------------------------------------------
# Cassette
# ---------------------------------------------------------------------------


class Cassette:
    """An ordered, replayable list of recorded interactions.

    The cassette is thread-safe so it can back the HTTP mock (one handler
    thread per request) as well as the in-process mock.  ``replay`` consumes
    the first matching, non-reusable interaction; reusable interactions remain
    available so idempotent lookups (``GetConfiguration``) serve every call.
    """

    def __init__(self, interactions: Iterable[Interaction] | None = None,
                 name: str | None = None, source: str | None = None):
        self._interactions: list[Interaction] = list(interactions or [])
        self.name = name
        self.source = source
        self._lock = threading.RLock()
        self._frozen = False

    # -- construction -----------------------------------------------------

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "Cassette":
        p = Path(path)
        with p.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        name = p.stem
        if isinstance(raw, Mapping) and isinstance(raw.get("name"), str):
            name = raw["name"]
        interactions = load_interactions(raw)
        return cls(interactions, name=name, source=str(p))

    @classmethod
    def from_dict(cls, raw: Any, name: str | None = None) -> "Cassette":
        return cls(load_interactions(raw), name=name)

    def add(self, interaction: Interaction) -> None:
        with self._lock:
            if self._frozen:
                raise CassetteError("cassette is frozen; cannot add interactions")
            self._interactions.append(interaction)

    def freeze(self) -> "Cassette":
        """Lock the cassette against further additions (replay still consumes)."""
        with self._lock:
            self._frozen = True
        return self

    def reset(self) -> None:
        """Mark every interaction as not-consumed (replay from the start)."""
        with self._lock:
            for ix in self._interactions:
                ix.consumed = False

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        return len(self._interactions)

    @property
    def interactions(self) -> list[Interaction]:
        """Snapshot of interactions (copies the list, not the items)."""
        with self._lock:
            return list(self._interactions)

    def signatures(self) -> set[str]:
        with self._lock:
            return {ix.signature for ix in self._interactions}

    # -- replay -----------------------------------------------------------

    def replay(self, query: str,
               variables: Mapping[str, Any] | None = None) -> Interaction:
        """Return the next matching interaction or raise ``CassetteNotFoundError``.

        The first matching interaction is returned.  If it is not reusable it
        is marked consumed so a subsequent identical call advances to the next
        recorded interaction in order -- this is how a cassette encodes a
        ``429`` immediately followed by a ``200``.
        """
        sig = signature_for_query(query)
        with self._lock:
            for ix in self._interactions:
                if ix.consumed:
                    continue
                if ix.signature != sig:
                    continue
                if ix.match_variables and not _vars_subset(
                    ix.variables or {}, variables or {}
                ):
                    continue
                if not ix.reusable:
                    ix.consumed = True
                return ix
            # Fall back to reusable interactions even if consumed.
            for ix in self._interactions:
                if ix.reusable and ix.signature == sig:
                    if not ix.match_variables or _vars_subset(
                        ix.variables or {}, variables or {}
                    ):
                        return ix
        raise CassetteNotFoundError(
            f"no unconsumed interaction for signature {sig!r} on cassette "
            f"{self.name!r} ({len(self._interactions)} total)"
        )

    def has_match(self, query: str,
                  variables: Mapping[str, Any] | None = None) -> bool:
        try:
            self.replay(query, variables)
        except CassetteNotFoundError:
            return False
        return True


def _vars_subset(needle: Mapping[str, Any], haystack: Mapping[str, Any]) -> bool:
    for key, expected in needle.items():
        if key not in haystack:
            return False
        if haystack[key] != expected:
            return False
    return True


# ---------------------------------------------------------------------------
# Cassette library
# ---------------------------------------------------------------------------


class CassetteLibrary:
    """A directory of ``*.json`` cassette files keyed by stem.

    Provides ``load(name)`` and ``__getitem__`` semantics; a cached
    :class:`Cassette` is returned per name.  The default fixtures directory is
    ``tests/fixtures`` relative to this file.
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self._cache: dict[str, Cassette] = {}

    def __contains__(self, name: str) -> bool:
        return self._path_for(name).is_file()

    def __iter__(self) -> Iterator[str]:
        for p in sorted(self.root.glob("*.json")):
            yield p.stem

    def _path_for(self, name: str) -> Path:
        candidate = self.root / f"{name}.json"
        return candidate

    def names(self) -> list[str]:
        return [p.stem for p in sorted(self.root.glob("*.json"))]

    def load(self, name: str, refresh: bool = False) -> Cassette:
        path = self._path_for(name)
        if not path.is_file():
            raise CassetteNotFoundError(
                f"no cassette named {name!r} under {self.root}"
            )
        if refresh or name not in self._cache:
            self._cache[name] = Cassette.from_file(path)
        return self._cache[name]

    def __getitem__(self, name: str) -> Cassette:
        return self.load(name)


# ---------------------------------------------------------------------------
# Recorder (host use; never exercised by Tier-A tests)
# ---------------------------------------------------------------------------


class CassetteRecorder:
    """Proxy GraphQL calls to a live Stash URL and capture the responses.

    Tier-A tests never need this -- it exists so a developer can regenerate a
    cassette on a host with a real Stash (and real scraper credentials).  It
    deliberately uses only ``urllib`` from the standard library.
    """

    def __init__(self, target_url: str, out_dir: str | os.PathLike,
                 *, api_key: str | None = None, cookie: str | None = None,
                 timeout: float = 60.0):
        self.target_url = target_url
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.cookie = cookie
        self.timeout = timeout
        self._counter = 0

    def submit(self, query: str,
               variables: Mapping[str, Any] | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            self.target_url, data=body, method="POST",
            headers={"Content-Type": "application/json", **self._auth_headers()},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
                status = resp.status
                headers = dict(resp.headers.items())
        except urllib.error.HTTPError as exc:  # pragma: no cover - host only
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
        payload = json.loads(raw) if raw else {}
        interaction = {
            "request": {"query": query, "variables": dict(variables or {})},
            "response": {
                "data": payload.get("data"),
                "errors": payload.get("errors"),
                "http_status": status,
                "headers": headers,
            },
        }
        self._write(interaction)
        return interaction["response"]

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _write(self, interaction: dict) -> Path:
        self._counter += 1
        sig = signature_for_query(interaction["request"]["query"])
        path = self.out_dir / f"{self._counter:03d}-{sig}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(interaction, fh, indent=2, sort_keys=False)
        return path

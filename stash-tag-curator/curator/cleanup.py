"""Orphan-tag cleanup engine (Issue 9).

Implements the *safe-global* and *plugin-owned* orphan-tag cleanup as one
conservative, single-step operation run as the final phase of a curator
run.  Binding constraints:

* **Conservative candidates (Issue 9)** -- a tag whose name appears in
  ``rules.canonical_tag_names()``, in the fixed ``CURATOR:`` marker
  enumeration (D3/D6), or matches ``protected.prefixes`` /
  ``protected.tag_names`` is **never** a candidate regardless of its
  association counts.  The same applies to any tag with non-zero direct
  associations -- ``scene_count``, ``scene_marker_count``, ``image_count``,
  ``gallery_count``, ``performer_count``, ``studio_count``, ``group_count``,
  ``parent_count`` or ``child_count``.

* **Deletion audit** -- before every ``tagsDestroy`` call the engine
  records the tag's id, name, axis, and aliases in the ``tag_deletions``
  SQLite table.  The table is an audit record (what was removed, listed in
  the run result); there is no undo -- a wrong cleanup self-heals because
  the pipeline re-derives tags from provider data on the next run.

The public surface is:

* :class:`CleanupEngine`        -- compute -> journal -> destroy.
* :class:`CleanupReport`        -- execution result.
* :class:`TagCandidate`         -- one candidate tag + its counts snapshot.
* :func:`safe_global_orphans`   -- candidate selector for globally-orphan tags.
* :func:`plugin_owned_orphans`  -- candidate selector for plugin-owned tags.
* :func:`is_protected_name`     -- protected-prefix/name predicate.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from curator.graphql_queries import (
    FIND_TAGS_WITH_COUNTS,
    TAG_DESTROY_BULK,
)
from curator.processing import CURATOR_MARKERS
from curator.rules import Rules
from curator.state import StateDB

__all__ = [
    "CleanupEngine",
    "CleanupReport",
    "TagCandidate",
    "SCOPE_SAFE_GLOBAL",
    "SCOPE_PLUGIN_OWNED",
    "safe_global_orphans",
    "plugin_owned_orphans",
    "is_protected_name",
]


# ---------------------------------------------------------------------------
# Scope constants
# ---------------------------------------------------------------------------

SCOPE_SAFE_GLOBAL = "safe_global"
SCOPE_PLUGIN_OWNED = "plugin_owned"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 (the canonical wire format for this DB)."""
    return datetime.now(timezone.utc).isoformat()


#: Default page size when paginating ``findTags``.  Stash's real per_page cap
#: is large enough that one call usually suffices for the cleanup scope; the
#: engine still paginates so a multi-thousand-tag library works end-to-end.
_DEFAULT_TAG_PAGE_SIZE = 1000


def _protected_config(rules: Rules) -> dict[str, Any]:
    """Return the ``protected`` block from a :class:`Rules` instance.

    ``Rules`` indexes the canonical taxonomy + mappings at build time but
    does not expose the ``protected`` sub-mapping via a dedicated accessor.
    This helper centralises the (defensive) extraction so cleanup-callers do
    not reach into the rules internals directly.
    """
    raw = getattr(rules, "_raw", None)
    if not isinstance(raw, Mapping):
        return {}
    protected = raw.get("protected")
    if not isinstance(protected, Mapping):
        return {}
    return dict(protected)


def _protected_prefixes(rules: Rules) -> tuple[str, ...]:
    """Tuple of protected prefixes (e.g. ``"MANUAL:"``) -- never orphans."""
    protected = _protected_config(rules)
    raw = protected.get("prefixes")
    if not isinstance(raw, Sequence):
        return ()
    return tuple(str(p) for p in raw if isinstance(p, str))


def _protected_tag_names(rules: Rules) -> frozenset[str]:
    """Frozen set of protected exact tag names -- never orphans."""
    protected = _protected_config(rules)
    raw = protected.get("tag_names")
    if not isinstance(raw, Sequence):
        return frozenset()
    return frozenset(str(n) for n in raw if isinstance(n, str))


def is_protected_name(name: str, rules: Rules) -> bool:
    """True if ``name`` is canonical, a CURATOR marker, or protected.

    Centralises the three Issue-9 exclusion predicates so selectors,
    :class:`CleanupEngine` and tests share a single source of truth:

    1. exact match against ``rules.canonical_tag_names()``;
    2. exact match against the fixed ``CURATOR:`` marker enumeration (D3/D6);
    3. exact match against ``protected.tag_names``;
    4. prefix match against ``protected.prefixes`` (e.g. ``"MANUAL:"``).
    """
    if not isinstance(name, str) or not name:
        return False
    if name in rules.canonical_tag_names():
        return True
    if name in CURATOR_MARKERS:
        return True
    if name in _protected_tag_names(rules):
        return True
    return any(name.startswith(prefix) for prefix in _protected_prefixes(rules))


# ---------------------------------------------------------------------------
# Count extraction (defensive -- Stash returns ints, but a missing field is
# treated as zero rather than raising so a malformed response never blocks
# cleanup).
# ---------------------------------------------------------------------------


def _count(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool):  # bool is an int subclass; guard explicitly
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


#: The complete set of association-count fields.  ALL must be zero for a tag
#: to qualify as a *globally* orphaned candidate (Issue 9 + the plan's
#: "performer-only tags preserved" acceptance criterion).
_COUNT_FIELDS: tuple[str, ...] = (
    "scene_count",
    "scene_marker_count",
    "image_count",
    "gallery_count",
    "performer_count",
    "studio_count",
    "group_count",
    "parent_count",
    "child_count",
)


def _all_counts_zero(row: Mapping[str, Any]) -> bool:
    """True iff every direct association count is zero."""
    return all(_count(row, field) == 0 for field in _COUNT_FIELDS)


# ---------------------------------------------------------------------------
# Candidate + report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TagCandidate:
    """A single tag proposed for deletion.

    Captures the association snapshot the user reviewed during dry-run plus a
    short ``reason`` explaining why the tag qualified (audit trail).  Stored
    verbatim in the D20 ``tag_deletions`` row so undo can recreate metadata.
    """

    tag_id: str
    name: str
    scene_count: int = 0
    scene_marker_count: int = 0
    image_count: int = 0
    gallery_count: int = 0
    performer_count: int = 0
    studio_count: int = 0
    group_count: int = 0
    parent_count: int = 0
    child_count: int = 0
    aliases: list[str] = field(default_factory=list)
    axis: "str | None" = None
    reason: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, reason: str = "") -> "TagCandidate":
        """Build a candidate from a ``findTags`` row.

        The ``aliases`` field is optional -- Stash exposes it as a separate
        selection that cleanup does not request by default (we only need the
        count fields + parent/child for the orphan predicate).  When present
        on the row it is preserved so undo can recreate it.
        """
        aliases_raw = row.get("aliases")
        if isinstance(aliases_raw, list):
            aliases = [str(a) for a in aliases_raw if isinstance(a, (str, int))]
        else:
            aliases = []
        return cls(
            tag_id=str(row["id"]),
            name=str(row["name"]),
            scene_count=_count(row, "scene_count"),
            scene_marker_count=_count(row, "scene_marker_count"),
            image_count=_count(row, "image_count"),
            gallery_count=_count(row, "gallery_count"),
            performer_count=_count(row, "performer_count"),
            studio_count=_count(row, "studio_count"),
            group_count=_count(row, "group_count"),
            parent_count=_count(row, "parent_count"),
            child_count=_count(row, "child_count"),
            aliases=aliases,
            axis=None,
            reason=reason,
        )


@dataclass
class CleanupReport:
    """Result of an executed cleanup run."""

    run_id: str
    scope: str
    destroyed_count: int
    destroyed_tag_ids: list[str]
    deletion_row_ids: list[int]
    skipped: list[dict[str, Any]] = field(default_factory=list)
    executed_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": self.scope,
            "destroyed_count": self.destroyed_count,
            "destroyed_tag_ids": list(self.destroyed_tag_ids),
            "deletion_row_ids": list(self.deletion_row_ids),
            "skipped": list(self.skipped),
            "executed_at": self.executed_at,
        }


@dataclass
class UndoReport:
    """Result of :func:`undo_cleanup` (best-effort)."""

    cleanup_run_id: str
    restored: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    restored_at: str = field(default_factory=_now_iso)

    @property
    def restored_count(self) -> int:
        return len(self.restored)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


# ---------------------------------------------------------------------------
# Client protocol (duck-typed): any object whose ``submit`` mirrors the
# MockClient/GraphQLClient contract works here.
# ---------------------------------------------------------------------------


def _signature(query: str) -> str:
    """Return the GraphQL operation name embedded in ``query``.

    Mirrors :func:`tests.harness.cassette.signature_for_query` so the same
    cassette-routing logic works against :class:`MockClient`.  An anonymous
    query falls back to ``"Anonymous"``.
    """
    import re

    stripped = "\n".join(
        line for line in query.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    match = re.search(
        r"(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)",
        stripped,
    )
    return match.group(1) if match else "Anonymous"


def _find_tags_with_counts(
    client: Any,
    *,
    page_size: int = _DEFAULT_TAG_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Page through ``findTags`` returning every tag row with its counts.

    The pagination loop is bounded by Stash's reported ``count`` and stops
    as soon as a page returns no rows (defensive against a count/contents
    skew).  Each row carries the raw tag id/name + every count field
    requested by :data:`FIND_TAGS_WITH_COUNTS`.
    """
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        data = client.submit(
            FIND_TAGS_WITH_COUNTS,
            {
                "filter": {"page": page, "per_page": page_size},
                "tag_filter": None,
                "ids": None,
            },
        )
        find_tags = (data or {}).get("findTags") or {}
        page_rows = find_tags.get("tags") or []
        if not page_rows:
            break
        for row in page_rows:
            if isinstance(row, Mapping):
                rows.append(dict(row))
        total = find_tags.get("count")
        if isinstance(total, int) and len(rows) >= total:
            break
        if len(page_rows) < page_size:
            break
        page += 1
        # Hard ceiling so a buggy mock/server cannot trap us in an infinite
        # pagination loop.
        if page > 100_000:
            break
    return rows


# ---------------------------------------------------------------------------
# Candidate selectors (module-level, per the plan)
# ---------------------------------------------------------------------------


def safe_global_orphans(
    client: Any,
    rules: Rules,
    *,
    page_size: int = _DEFAULT_TAG_PAGE_SIZE,
) -> list[TagCandidate]:
    """Return every globally-orphaned tag in the library.

    A tag is a *safe global orphan* iff:

    * every direct association count is zero (``scene_count``,
      ``scene_marker_count``, ``image_count``, ``gallery_count``,
      ``performer_count``, ``studio_count``, ``group_count``,
      ``parent_count``, ``child_count`` -- Issue 9);
    * AND its name is NOT canonical (``rules.canonical_tag_names()``);
    * AND its name is NOT a fixed ``CURATOR:`` marker (D3/D6 enumeration);
    * AND its name does NOT match ``protected.prefixes`` or
      ``protected.tag_names``.

    The selector never destroys anything -- it only computes the candidate
    list.  Pass the result to :meth:`CleanupEngine.propose` /
    :meth:`CleanupEngine.dry_run` to obtain a confirmation token, then to
    :meth:`CleanupEngine.execute_cleanup` to act.
    """
    canonical = frozenset(rules.canonical_tag_names())
    markers = frozenset(CURATOR_MARKERS)
    protected_names = _protected_tag_names(rules)
    protected_prefixes = _protected_prefixes(rules)

    candidates: list[TagCandidate] = []
    for row in _find_tags_with_counts(client, page_size=page_size):
        name = str(row.get("name") or "")
        if not name:
            continue
        if name in canonical:
            continue
        if name in markers:
            continue
        if name in protected_names:
            continue
        if any(name.startswith(p) for p in protected_prefixes):
            continue
        if not _all_counts_zero(row):
            continue
        candidates.append(
            TagCandidate.from_row(
                row,
                reason="all direct association counts are zero",
            )
        )
    return candidates


def plugin_owned_orphans(
    client: Any,
    rules: Rules,
    *,
    page_size: int = _DEFAULT_TAG_PAGE_SIZE,
) -> list[TagCandidate]:
    """Return every plugin-owned orphaned tag.

    A tag is a *plugin-owned orphan* iff its name starts with ``"CURATOR:"``
    OR it matches a *former* canonical tag name that is no longer present in
    the active canonical set.  The latter is computed defensively: since the
    active rules file carries no explicit "former canonical" list, a tag is
    considered a former-canonical candidate when it carries the
    ``"CURATOR:"`` prefix OR matches a canonical name from the **prefix
    table** (i.e. it looks like a generated canonical tag) but is absent
    from the current ``rules.canonical_tag_names()`` enumeration AND has all
    association counts zero.

    The CURATOR markers themselves (D3/D6 enumeration) are always excluded
    from *this* selector too -- :func:`safe_global_orphans` already excludes
    them, and ``plugin_owned_orphans`` is for *stale* plugin tags, not the
    fixed lifecycle markers.  A CURATOR marker is only ever removed by
    re-running cleanup on a tag whose associations have all been moved
    elsewhere, which falls under the safe-global path.
    """
    canonical = frozenset(rules.canonical_tag_names())
    protected_names = _protected_tag_names(rules)
    protected_prefixes = _protected_prefixes(rules)

    candidates: list[TagCandidate] = []
    for row in _find_tags_with_counts(client, page_size=page_size):
        name = str(row.get("name") or "")
        if not name:
            continue
        # The fixed CURATOR markers are never plugin-owned-orphans -- they
        # are lifecycle tags managed by the processing engine (D3/D6).
        if name in CURATOR_MARKERS:
            continue
        is_curator_prefixed = name.startswith("CURATOR:")
        # "Former canonical" heuristic: a tag that parses as a canonical
        # axis tag (prefix from the rules table) but is NOT in the current
        # canonical set.  We only consider it a candidate when it has zero
        # direct associations -- otherwise it is still in active use and
        # must not be cleaned up.
        former_canonical = False
        axis = rules.axis_for(name)
        if axis is not None and name not in canonical:
            former_canonical = True
        if not (is_curator_prefixed or former_canonical):
            continue
        # Protected tags are never candidates -- even plugin-owned ones.
        # (A user can protect a CURATOR: tag explicitly to pin it.)
        if name in protected_names:
            continue
        if any(name.startswith(p) for p in protected_prefixes):
            continue
        # Plugin-owned tags must ALSO have all counts zero for cleanup --
        # a CURATOR marker still attached to a scene is not an orphan.
        if not _all_counts_zero(row):
            continue
        reason_parts = []
        if is_curator_prefixed:
            reason_parts.append("CURATOR: prefix (plugin-owned)")
        if former_canonical:
            reason_parts.append(f"former canonical (axis={axis})")
        candidates.append(
            TagCandidate.from_row(
                row,
                reason="; ".join(reason_parts) if reason_parts else "plugin-owned",
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# CleanupEngine
# ---------------------------------------------------------------------------


class CleanupEngine:
    """Removes orphaned tags in one step: compute -> journal -> destroy.

    There is no dry-run/execute split and no confirmation token: cleanup
    runs as the final phase of a curator run, and its candidate predicates
    are conservative by construction (every association count must be zero
    everywhere; canonical and protected tags are never candidates).  What
    was deleted is recorded in the ``tag_deletions`` audit table and listed
    in the run result; a wrong cleanup is repaired by re-deriving (the tags
    reappear on the next run if the provider data still calls for them).

    * ``client``  -- duck-typed GraphQL client (``submit`` returns ``data``);
    * ``state``   -- :class:`~curator.state.StateDB` for the deletion audit;
    * ``rules``   -- :class:`~curator.rules.Rules` for canonical/protected
      exclusion.
    """

    def __init__(
        self,
        client: Any,
        state: StateDB,
        rules: Rules,
        *,
        run_id: "str | None" = None,
    ) -> None:
        self.client = client
        self.state = state
        self.rules = rules
        self.run_id = run_id or f"cleanup-{secrets.token_hex(8)}"

    def run(
        self,
        scope: str,
        *,
        exclude: "Sequence[str] | None" = None,
        exclude_names: "Sequence[str] | None" = None,
    ) -> CleanupReport:
        """Find, journal, and destroy orphaned tags for ``scope``.

        ``scope`` is one of :data:`SCOPE_SAFE_GLOBAL` or
        :data:`SCOPE_PLUGIN_OWNED`.  ``exclude`` is an optional iterable of
        tag *ids* to spare.  ``exclude_names`` is an optional iterable of
        tag *names* to spare -- the caller passes the engine's finite
        tag-candidate set here so the D6 pre-pass and cleanup never fight
        (the pre-pass re-creates every tag the engine may emit; destroying
        the unused ones would churn hundreds of tags every run).  An empty
        candidate list short-circuits: no ``tag_deletions`` rows, no
        ``tagsDestroy`` call.

        If the bulk destroy call fails, the audit rows remain (a faithful
        record of intent) and the failure is surfaced in the report's
        ``skipped`` list -- the next run retries the same candidates.
        """
        if scope == SCOPE_SAFE_GLOBAL:
            candidates = safe_global_orphans(self.client, self.rules)
        elif scope == SCOPE_PLUGIN_OWNED:
            candidates = plugin_owned_orphans(self.client, self.rules)
        else:
            raise ValueError(
                f"unknown cleanup scope: {scope!r} "
                f"(expected {SCOPE_SAFE_GLOBAL!r} or {SCOPE_PLUGIN_OWNED!r})"
            )

        excluded_ids = {str(t) for t in (exclude or ())}
        protected_names = {str(n) for n in (exclude_names or ())}
        candidates = [
            c for c in candidates
            if c.tag_id not in excluded_ids and c.name not in protected_names
        ]

        ids_to_destroy = [c.tag_id for c in candidates]
        if not ids_to_destroy:
            return CleanupReport(
                run_id=self.run_id,
                scope=scope,
                destroyed_count=0,
                destroyed_tag_ids=[],
                deletion_row_ids=[],
            )

        # Audit rows BEFORE the destroy call (name/axis survive the destroy;
        # the row records what was removed even if the call fails).
        deletion_row_ids = self._record_deletions(candidates)

        try:
            self.client.submit(
                TAG_DESTROY_BULK,
                {"ids": ids_to_destroy},
            )
        except Exception as exc:
            return CleanupReport(
                run_id=self.run_id,
                scope=scope,
                destroyed_count=0,
                destroyed_tag_ids=[],
                deletion_row_ids=deletion_row_ids,
                skipped=[
                    {
                        "phase": "tagsDestroy",
                        "error": str(exc),
                        "tag_ids": list(ids_to_destroy),
                    }
                ],
            )

        return CleanupReport(
            run_id=self.run_id,
            scope=scope,
            destroyed_count=len(ids_to_destroy),
            destroyed_tag_ids=ids_to_destroy,
            deletion_row_ids=deletion_row_ids,
        )

    # ------------------------------------------------------------------
    # Deletion audit
    # ------------------------------------------------------------------

    def _record_deletions(
        self,
        candidates: Sequence[TagCandidate],
    ) -> list[int]:
        """Write one ``tag_deletions`` audit row per candidate; return row ids.

        Each row stores: ``run_id, tag_id, tag_name, axis, parent_ids_json,
        child_ids_json, aliases_json, deleted_at`` (the proposal-token column
        stays NULL -- it is a legacy of the retired two-phase flow).
        """
        now = _now_iso()
        row_ids: list[int] = []
        with self.state._txn():
            for c in candidates:
                cur = self.state.connection.execute(
                    "INSERT INTO tag_deletions "
                    "(run_id, tag_id, tag_name, axis, parent_ids_json, "
                    " child_ids_json, aliases_json, deletion_proposal_token, "
                    " deleted_at, restored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                    (
                        self.run_id,
                        int(c.tag_id) if str(c.tag_id).isdigit() else None,
                        c.name,
                        c.axis,
                        json.dumps([]),
                        json.dumps([]),
                        json.dumps(list(c.aliases)),
                        now,
                    ),
                )
                if cur.lastrowid is not None:
                    row_ids.append(int(cur.lastrowid))
        return row_ids

"""Orphan-tag cleanup engine (T18 / D19 / D20 / Issue 9).

This module implements the *safe* and *plugin-owned* orphan-tag cleanup
operations plus their best-effort undo.  The design obeys three binding
constraints from the stash-tag-curator plan:

* **D19 (proposal required)** -- no tag is ever destroyed without a prior
  dry-run proposal the user reviewed.  ``dry_run`` produces a
  :class:`CleanupProposal` carrying an opaque token; ``execute_cleanup``
  refuses to act unless that exact token is presented.

* **D20 (tag-deletion journal)** -- before every ``tagsDestroy`` call the
  engine records the tag's metadata (id, name, axis, parents, children,
  aliases, proposal token) in the ``tag_deletions`` SQLite table.  That row
  is what :func:`undo_cleanup` reads to recreate a deleted tag.

* **Issue 9 (canonical/markers never orphaned)** -- a tag whose name appears
  in ``rules.canonical_tag_names()``, in the fixed ``CURATOR:`` marker
  enumeration (D3/D6), or matches ``protected.prefixes`` /
  ``protected.tag_names`` is **never** a candidate regardless of its
  association counts.  The same applies to any tag with non-zero direct
  associations -- ``scene_count``, ``scene_marker_count``, ``image_count``,
  ``gallery_count``, ``performer_count``, ``studio_count``, ``group_count``,
  ``parent_count`` or ``child_count``.

The module is deliberately free of any cancellation-flag wiring: cleanup is
a short, bounded operation (one find + one bulk destroy) and Stash
cancellation flows through ``stopJob -> SIGKILL`` (D5) which leaves a stale
lock the operator force-releases.

The public surface is:

* :class:`CleanupEngine`        -- proposal/execute orchestrator.
* :class:`CleanupProposal`      -- dry-run result + confirmation token.
* :class:`CleanupReport`        -- execute result.
* :class:`UndoReport`           -- undo result.
* :func:`safe_global_orphans`   -- candidate selector for globally-orphan tags.
* :func:`plugin_owned_orphans`  -- candidate selector for plugin-owned tags.
* :func:`undo_cleanup`          -- best-effort tag re-creation (D20).
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
    TAG_CREATE,
    TAG_DESTROY_BULK,
)
from curator.processing import CURATOR_MARKERS
from curator.rules import Rules
from curator.state import StateDB

__all__ = [
    "CleanupEngine",
    "CleanupProposal",
    "CleanupReport",
    "UndoReport",
    "TagCandidate",
    "SCOPE_SAFE_GLOBAL",
    "SCOPE_PLUGIN_OWNED",
    "safe_global_orphans",
    "plugin_owned_orphans",
    "undo_cleanup",
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
class CleanupProposal:
    """Dry-run result + confirmation token (D19).

    The token is an opaque 256-bit hex value; ``execute_cleanup`` requires
    the exact token produced by the dry-run the user reviewed.  The engine
    retains proposals in-memory keyed by token, so a token issued by one
    :class:`CleanupEngine` instance cannot be executed by another.
    """

    token: str
    scope: str
    candidates: list[TagCandidate]
    created_at: str = field(default_factory=_now_iso)
    rules_sha: "str | None" = None

    @property
    def candidate_ids(self) -> list[str]:
        return [c.tag_id for c in self.candidates]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (for reporting / asset snapshots)."""
        return {
            "token": self.token,
            "scope": self.scope,
            "created_at": self.created_at,
            "rules_sha": self.rules_sha,
            "candidate_count": len(self.candidates),
            "candidates": [
                {
                    **asdict(c),
                    "tag_id": c.tag_id,
                }
                for c in self.candidates
            ],
        }


@dataclass
class CleanupReport:
    """Result of an executed cleanup run."""

    run_id: str
    scope: str
    proposal_token: str
    destroyed_count: int
    destroyed_tag_ids: list[str]
    deletion_row_ids: list[int]
    skipped: list[dict[str, Any]] = field(default_factory=list)
    executed_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": self.scope,
            "proposal_token": self.proposal_token,
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
    """Orchestrates dry-run + execute for orphan-tag cleanup.

    The engine is the only public entry point that mutates state.  It holds:

    * ``client``  -- duck-typed GraphQL client (``submit`` returns ``data``);
    * ``state``   -- :class:`~curator.state.StateDB` for the D20 journal;
    * ``rules``   -- :class:`~curator.rules.Rules` for canonical/protected
      exclusion.

    A confirmation token (256-bit hex) is generated per dry-run and stored
    in ``self._proposals``; ``execute_cleanup`` looks the token up there.
    Tokens are single-use: once executed the proposal is removed so a
    replay cannot destroy a second batch.
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
        self._proposals: dict[str, CleanupProposal] = {}

    # ------------------------------------------------------------------
    # Dry-run
    # ------------------------------------------------------------------

    def dry_run(
        self,
        scope: str,
        *,
        exclude: "Sequence[str] | None" = None,
    ) -> CleanupProposal:
        """Compute the candidate list for ``scope`` and return a proposal.

        ``scope`` is one of :data:`SCOPE_SAFE_GLOBAL` or
        :data:`SCOPE_PLUGIN_OWNED`.  ``exclude`` is an optional iterable of
        tag *ids* to remove from the proposal (the user's per-tag veto).
        The returned :class:`CleanupProposal` carries a fresh single-use
        token that must be passed to :meth:`execute_cleanup`.

        Raises :class:`ValueError` on an unknown scope.
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
        if excluded_ids:
            candidates = [c for c in candidates if c.tag_id not in excluded_ids]

        token = secrets.token_hex(16)
        proposal = CleanupProposal(
            token=token,
            scope=scope,
            candidates=candidates,
            rules_sha=getattr(self.rules, "rules_sha", None),
        )
        self._proposals[token] = proposal
        return proposal

    #: Alias for the spec-required method name.
    def propose(
        self,
        scope: str,
        *,
        exclude: "Sequence[str] | None" = None,
    ) -> CleanupProposal:
        """Alias for :meth:`dry_run` (spec mentions both names)."""
        return self.dry_run(scope, exclude=exclude)

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute_cleanup(
        self,
        proposal_token: str,
        *,
        exclude: "Sequence[str] | None" = None,
    ) -> CleanupReport:
        """Execute the proposal identified by ``proposal_token``.

        Steps (D20 + D19):

        1. Resolve the token -> :class:`CleanupProposal`.  Unknown or
           already-consumed tokens raise :class:`LookupError`.
        2. Apply any additional ``exclude`` ids (last-minute veto).
        3. Write one ``tag_deletions`` row PER candidate BEFORE the destroy
           call.  Each row captures the metadata :func:`undo_cleanup` needs.
        4. Call ``tagsDestroy`` ONCE with the full id list (bulk).
        5. Mark the proposal consumed (removed from ``self._proposals``) and
           return a :class:`CleanupReport`.

        If the candidate list is empty the engine short-circuits: no
        ``tag_deletions`` rows, no ``tagsDestroy`` call, empty report.
        """
        proposal = self._proposals.get(proposal_token)
        if proposal is None:
            raise LookupError(
                f"unknown or already-consumed cleanup proposal token: "
                f"{proposal_token!r}"
            )

        excluded_ids = {str(t) for t in (exclude or ())}
        candidates = [
            c for c in proposal.candidates if c.tag_id not in excluded_ids
        ]

        ids_to_destroy = [c.tag_id for c in candidates]
        deletion_row_ids: list[int] = []
        skipped: list[dict[str, Any]] = []

        if not ids_to_destroy:
            # Nothing to do -- consume the token so it cannot be replayed.
            self._proposals.pop(proposal_token, None)
            return CleanupReport(
                run_id=self.run_id,
                scope=proposal.scope,
                proposal_token=proposal_token,
                destroyed_count=0,
                destroyed_tag_ids=[],
                deletion_row_ids=[],
                skipped=skipped,
            )

        # --- D20: write tag_deletions rows BEFORE the destroy call. ---
        deletion_row_ids = self._record_deletions(
            candidates,
            proposal_token=proposal_token,
        )

        # --- Bulk destroy (single call, full id list). ---
        try:
            self.client.submit(
                TAG_DESTROY_BULK,
                {"ids": ids_to_destroy},
            )
        except Exception as exc:
            # The destroy failed.  Leave the tag_deletions rows in place --
            # they are still a faithful audit of the INTENT and the caller
            # can retry.  Mark them with a NULL restored_at (already NULL)
            # and surface the failure in the report.
            skipped.append(
                {
                    "phase": "tagsDestroy",
                    "error": str(exc),
                    "tag_ids": list(ids_to_destroy),
                }
            )
            self._proposals.pop(proposal_token, None)
            return CleanupReport(
                run_id=self.run_id,
                scope=proposal.scope,
                proposal_token=proposal_token,
                destroyed_count=0,
                destroyed_tag_ids=[],
                deletion_row_ids=deletion_row_ids,
                skipped=skipped,
            )

        # --- Success: consume the token. ---
        self._proposals.pop(proposal_token, None)

        return CleanupReport(
            run_id=self.run_id,
            scope=proposal.scope,
            proposal_token=proposal_token,
            destroyed_count=len(ids_to_destroy),
            destroyed_tag_ids=ids_to_destroy,
            deletion_row_ids=deletion_row_ids,
            skipped=skipped,
        )

    # ------------------------------------------------------------------
    # D20 journal helpers
    # ------------------------------------------------------------------

    def _record_deletions(
        self,
        candidates: Sequence[TagCandidate],
        *,
        proposal_token: str,
    ) -> list[int]:
        """Write one ``tag_deletions`` row per candidate; return their row ids.

        Each row stores: ``run_id, tag_id, tag_name, axis, parent_ids_json,
        child_ids_json, aliases_json, deletion_proposal_token, deleted_at``.
        ``restored_at`` is left NULL -- :func:`undo_cleanup` populates it on
        successful re-creation.

        Parents/children ids are NOT requested in the default
        :data:`FIND_TAGS_WITH_COUNTS` selection (we only need counts for the
        orphan predicate); the candidate therefore carries empty lists and
        the row stores ``"[]"``.  Callers that need richer undo metadata can
        extend the candidate via :meth:`TagCandidate.from_row` with a row
        that includes parents/children.
        """
        now = _now_iso()
        row_ids: list[int] = []
        with self.state._txn():
            for c in candidates:
                parent_ids: list[str] = []
                child_ids: list[str] = []
                aliases = list(c.aliases)
                cur = self.state.connection.execute(
                    "INSERT INTO tag_deletions "
                    "(run_id, tag_id, tag_name, axis, parent_ids_json, "
                    " child_ids_json, aliases_json, deletion_proposal_token, "
                    " deleted_at, restored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        self.run_id,
                        int(c.tag_id) if str(c.tag_id).isdigit() else None,
                        c.name,
                        c.axis,
                        json.dumps(parent_ids),
                        json.dumps(child_ids),
                        json.dumps(aliases),
                        proposal_token,
                        now,
                    ),
                )
                if cur.lastrowid is not None:
                    row_ids.append(int(cur.lastrowid))
        return row_ids

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def pending_proposal(self, token: str) -> "CleanupProposal | None":
        """Return the in-memory proposal for ``token`` (or ``None``)."""
        return self._proposals.get(token)


# ---------------------------------------------------------------------------
# UndoCleanup (D20 restoration)
# ---------------------------------------------------------------------------


def undo_cleanup(
    client: Any,
    state: StateDB,
    cleanup_run_id: str,
    *,
    run_id: "str | None" = None,
) -> UndoReport:
    """Best-effort restoration of every tag destroyed by ``cleanup_run_id``.

    Reads the ``tag_deletions`` rows for ``cleanup_run_id`` where
    ``restored_at IS NULL`` (so a repeat call is a no-op for already
    restored tags), then re-creates each tag via ``tagCreate``.  **Tag IDs
    will differ** -- Stash assigns a fresh primary key on creation -- so the
    report carries both the old id (from the journal) and the new id
    returned by ``tagCreate``.

    Failures (network errors, validation errors, etc.) are recorded per-tag
    in ``UndoReport.failed`` without aborting the loop.  Successfully
    restored rows have their ``restored_at`` timestamp written so a repeat
    invocation skips them.
    """
    restoring_run_id = run_id or f"undo-cleanup-{secrets.token_hex(8)}"
    report = UndoReport(cleanup_run_id=cleanup_run_id)

    rows = state.connection.execute(
        "SELECT * FROM tag_deletions "
        "WHERE run_id = ? AND restored_at IS NULL "
        "ORDER BY id",
        (cleanup_run_id,),
    ).fetchall()

    for row in rows:
        row_id = int(row["id"])
        old_tag_id = row["tag_id"]
        name = row["tag_name"]
        if not name:
            report.failed.append(
                {
                    "row_id": row_id,
                    "old_tag_id": old_tag_id,
                    "error": "missing tag_name in tag_deletions row",
                }
            )
            continue

        aliases_raw = row["aliases_json"]
        try:
            aliases = json.loads(aliases_raw) if aliases_raw else []
        except (ValueError, TypeError):
            aliases = []

        # Build the TagCreateInput.  Stash accepts name + optional aliases
        # + parent/child id lists.  Since the original ids are stale we
        # cannot re-link hierarchy; undo restores the name + aliases only.
        create_input: dict[str, Any] = {"name": name}
        if aliases:
            create_input["aliases"] = list(aliases)

        try:
            data = client.submit(TAG_CREATE, {"input": create_input})
        except Exception as exc:
            report.failed.append(
                {
                    "row_id": row_id,
                    "old_tag_id": old_tag_id,
                    "name": name,
                    "error": str(exc),
                }
            )
            continue

        created = (data or {}).get("tagCreate") or {}
        new_id = created.get("id")
        if new_id is None:
            report.failed.append(
                {
                    "row_id": row_id,
                    "old_tag_id": old_tag_id,
                    "name": name,
                    "error": "tagCreate returned no id",
                }
            )
            continue

        # Mark restored -- single-statement UPDATE autocommits under
        # isolation_level=None; safe for a one-row write.
        state.connection.execute(
            "UPDATE tag_deletions SET restored_at = ? WHERE id = ?",
            (_now_iso(), row_id),
        )
        report.restored.append(
            {
                "row_id": row_id,
                "old_tag_id": old_tag_id,
                "new_tag_id": str(new_id),
                "name": name,
                "aliases": aliases,
            }
        )

    return report

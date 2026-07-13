"""Scene-metadata enrichment helpers (Milestone 1).

This module implements the **fill-empty-only** metadata policy (plan §2.1, §5.2):

* A scraped value may be written only when the scene's current field is empty.
* Emptiness is evaluated by a centralised per-field predicate (not scattered
  truthiness checks), matching G3-verified Stash storage semantics.
* The diff is computed at dry-run time and re-evaluated at execute time (the
  scene may have been edited between the two phases).

The module is import-safe on a host with no Stash (stdlib + :mod:`curator.providers`
dataclasses only).

Design contracts (from the approved plan + G3/G4 findings):

* **Fill-empty-only** (§2.1): never overwrite a non-empty value, even if the
  scrape is more complete or the provider is higher priority.
* **Per-field** (§5.2): emptiness is evaluated independently for each field.
* **Performers are atomic** (§5.4): the performer list is proposed only if the
  scene currently has NO performers; the entire list is applied or nothing.
* **No duration** (G1): ``SceneUpdateInput`` has no writable ``duration``
  field, so it is excluded from the metadata model entirely.
* **Provenance preserved** (§5.1): every proposed field records the source
  endpoint so diagnostics can show where the value came from.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .providers import MetadataField, SceneMetadata, ScrapedEntity

__all__ = [
    "METADATA_SCHEMA_VERSION",
    "is_field_empty",
    "compute_fill_empty_diff",
    "reevaluate_diff_at_execute",
    "diff_to_update_fields",
    "build_applied_result",
    "scene_current_metadata",
    "diff_to_json",
    "diff_from_json",
]

#: Version of the proposed_metadata_json / applied_metadata_json blob shape.
#: Bumped if the structure changes.  Stored alongside the data so old rows
#: can be migrated or rejected cleanly.
METADATA_SCHEMA_VERSION = 1

#: The scalar scene fields eligible for fill-empty enrichment (plan §5.2).
#: ``duration`` is intentionally absent (G1: not writable via SceneUpdateInput).
#: ``urls`` is a list field handled separately.  ``performers`` and ``studio``
#: are entity fields handled separately (Workstream B resolution).
_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "date",
    "code",
    "details",
    "director",
)


def _is_scalar_empty(value: Any) -> bool:
    """Return True if a scalar field value is considered empty.

    Per G3: Stash stores both ``null`` and ``""`` as empty string.  We treat
    ``None``, ``""``, and whitespace-only strings as empty.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def is_field_empty(field_name: str, scene: Mapping[str, Any]) -> bool:
    """Centralised per-field emptiness predicate (plan §5.2).

    Evaluates the scene's CURRENT value for the given field against the
    emptiness rules.  Used at both dry-run and execute time.

    Parameters
    ----------
    field_name
        One of ``title``, ``date``, ``code``, ``details``, ``director``,
        ``urls``, ``studio``, ``performers``.
    scene
        The current scene dict (as returned by ``findScenes`` / ``findScene``).
    """
    if field_name in _SCALAR_FIELDS:
        return _is_scalar_empty(scene.get(field_name))
    if field_name == "urls":
        raw = scene.get("urls")
        if not isinstance(raw, list):
            return True
        return len(raw) == 0
    if field_name == "studio":
        studio = scene.get("studio")
        if not isinstance(studio, Mapping):
            return True
        return studio.get("id") is None
    if field_name == "performers":
        raw = scene.get("performers")
        if not isinstance(raw, list):
            return True
        return len(raw) == 0
    # Unknown field: be conservative, treat as non-empty (don't fill).
    return False


def scene_current_metadata(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the current mutable metadata fields from a scene dict.

    Returns a dict keyed by field name with the scene's current value (for
    journaling / rollback comparison).  Only the fill-empty-eligible fields
    are included.
    """
    out: dict[str, Any] = {}
    for f in _SCALAR_FIELDS:
        out[f] = scene.get(f)
    out["urls"] = scene.get("urls")
    studio = scene.get("studio")
    out["studio_id"] = (
        str(studio["id"]) if isinstance(studio, Mapping) and studio.get("id") else None
    )
    performers = scene.get("performers")
    out["performer_ids"] = (
        [str(p["id"]) for p in performers if isinstance(p, Mapping) and p.get("id")]
        if isinstance(performers, list)
        else []
    )
    return out


def compute_fill_empty_diff(
    scene: Mapping[str, Any],
    metadata: "SceneMetadata | None",
) -> dict[str, Any]:
    """Compute the fill-empty proposal diff for one scene.

    For each field in the scraped :class:`SceneMetadata`, the field is
    included in the diff ONLY IF the scene's current value is empty (per
    :func:`is_field_empty`).  Non-empty current values are never overwritten
    (plan §2.1).

    Returns a versioned diff dict::

        {
            "schema_version": 1,
            "scene_metadata_baseline": "...",  # fingerprint of all mutable fields at dry-run time
            "fields": {
                "title": {
                    "old": null,            # current scene value
                    "new": "Scraped Title", # proposed value
                    "source_endpoint": "https://stashdb.org/graphql"
                },
                ...
            },
            "entities": {
                "performers": [             # ScrapedEntity list (resolution deferred to execute)
                    {"stored_id": "43", "name": "...", "remote_site_id": "...", "endpoint": "..."}
                ],
                "studio": {"stored_id": null, "name": "...", "remote_site_id": "...", "endpoint": "..."}
            }
        }

    An empty ``fields`` dict + empty ``entities`` means no metadata changes
    are proposed for this scene (the scene is already fully populated, or the
    scrape supplied nothing useful).

    Performers and studio are included as entity *intents* (not resolved ids)
    because Workstream B resolution happens at execute time.  They are only
    included if the scene's current performers/studio are empty.
    """
    diff: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "scene_metadata_baseline": None,  # set by caller with _scene_metadata_fp
        "fields": {},
        "entities": {"performers": [], "studio": None},
    }
    if metadata is None:
        return diff

    # Scalar fields
    for fname in _SCALAR_FIELDS:
        scraped: "MetadataField | None" = getattr(metadata, fname)
        if scraped is None:
            continue
        if is_field_empty(fname, scene):
            diff["fields"][fname] = {
                "old": scene.get(fname),
                "new": scraped.value,
                "source_endpoint": scraped.source_endpoint,
            }

    # urls (list field)
    if metadata.urls is not None and is_field_empty("urls", scene):
        diff["fields"]["urls"] = {
            "old": scene.get("urls"),
            "new": metadata.urls.value,
            "source_endpoint": metadata.urls.source_endpoint,
        }

    # studio (entity — included only if scene has no studio)
    if metadata.studio is not None and is_field_empty("studio", scene):
        diff["entities"]["studio"] = {
            "stored_id": metadata.studio.stored_id,
            "name": metadata.studio.name,
            "remote_site_id": metadata.studio.remote_site_id,
            "endpoint": metadata.studio.endpoint,
        }

    # performers (entity list — included only if scene has no performers)
    # Atomic: the entire scraped performer list is proposed or nothing.
    if metadata.performers and is_field_empty("performers", scene):
        diff["entities"]["performers"] = [
            {
                "stored_id": p.stored_id,
                "name": p.name,
                "remote_site_id": p.remote_site_id,
                "endpoint": p.endpoint,
            }
            for p in metadata.performers
        ]

    return diff


def diff_to_json(diff: dict[str, Any]) -> str:
    """Serialise a diff dict to a JSON string for DB persistence."""
    return json.dumps(diff, ensure_ascii=False, separators=(",", ":"))


def diff_from_json(raw: "str | None") -> "dict[str, Any] | None":
    """Deserialise a diff dict from a JSON string.

    Returns ``None`` if ``raw`` is ``None`` or empty (the column was not
    populated, e.g. pre-v2 rows or scenes with no metadata proposal).
    """
    if not raw:
        return None
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except (json.JSONDecodeError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Execute-time re-evaluation
# ---------------------------------------------------------------------------


def reevaluate_diff_at_execute(
    diff: Mapping[str, Any],
    scene: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-evaluate a proposed diff against the scene's CURRENT state.

    At execute time the scene may have been edited since the dry-run.  This
    function returns a *slimmed* diff containing only fields that are STILL
    eligible for fill-empty (plan §A4: per-field, do not fail unrelated
    fields for one change).

    For each proposed scalar/list field:
    * If the field is still empty → include it (status ``"eligible"``).
    * If the field is now non-empty → exclude it (it was filled externally;
      status ``"skipped_not_empty"`` in the applied result).

    For entity fields (performers/studio): same logic — only included if
    still empty.  Entity resolution (stored_id → local id, create) happens
    in Workstream B; here we just check eligibility.

    Returns a new diff dict with only the eligible fields/entities.
    """
    eligible: dict[str, Any] = {
        "schema_version": diff.get("schema_version", METADATA_SCHEMA_VERSION),
        "fields": {},
        "entities": {"performers": [], "studio": None},
    }
    skipped_not_empty: list[str] = []

    for fname, fdata in diff.get("fields", {}).items():
        if not isinstance(fdata, Mapping):
            continue
        if is_field_empty(fname, scene):
            eligible["fields"][fname] = dict(fdata)
        else:
            skipped_not_empty.append(fname)

    # Studio: only eligible if scene still has no studio.
    studio = diff.get("entities", {}).get("studio")
    if studio and is_field_empty("studio", scene):
        eligible["entities"]["studio"] = studio
    elif studio:
        skipped_not_empty.append("studio")

    # Performers: only eligible if scene still has no performers.
    performers = diff.get("entities", {}).get("performers", [])
    if performers and is_field_empty("performers", scene):
        eligible["entities"]["performers"] = list(performers)
    elif performers:
        skipped_not_empty.append("performers")

    eligible["_skipped_not_empty"] = skipped_not_empty
    return eligible


def diff_to_update_fields(
    eligible_diff: Mapping[str, Any],
    *,
    resolved_studio_id: "str | None" = None,
    resolved_performer_ids: "Sequence[str] | None" = None,
) -> dict[str, Any]:
    """Convert an eligible diff into ``sceneUpdate`` input fields.

    Scalar fields map directly (title → title, etc.).  ``urls`` is a list.
    Studio/performers use the RESOLVED local ids supplied by the caller
    (Workstream B entity resolution).  If ``resolved_studio_id`` is None but
    the diff has a studio entity, the studio field is omitted (resolution
    failed/deferred).  Same for performers.

    Returns a dict suitable for passing as ``metadata_fields`` to
    :meth:`RebuildEngine._scene_update`.
    """
    from collections.abc import Sequence as _Seq  # noqa: F811

    out: dict[str, Any] = {}
    for fname, fdata in eligible_diff.get("fields", {}).items():
        if isinstance(fdata, Mapping) and "new" in fdata:
            out[fname] = fdata["new"]

    if resolved_studio_id:
        out["studio_id"] = str(resolved_studio_id)
    if resolved_performer_ids is not None:
        out["performer_ids"] = [str(p) for p in resolved_performer_ids]

    return out


def build_applied_result(
    eligible_diff: Mapping[str, Any],
    update_fields: Mapping[str, Any],
    *,
    mutation_succeeded: bool,
) -> dict[str, Any]:
    """Build the ``applied_metadata_json`` result blob after a sceneUpdate.

    Records the per-field outcome: ``applied``, ``skipped_not_empty``, or
    ``failed`` (when the mutation itself failed).  Entity fields record
    whether the resolved id was applied.

    This blob is stored in ``dry_run_proposals.applied_metadata_json`` and
    used by reporting + rollback.
    """
    result: dict[str, Any] = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "fields": {},
        "entities": {"performers": "not_proposed", "studio": "not_proposed"},
    }

    skipped = eligible_diff.get("_skipped_not_empty", [])

    for fname in eligible_diff.get("fields", {}):
        if fname in skipped:
            result["fields"][fname] = "skipped_not_empty"
        elif mutation_succeeded:
            result["fields"][fname] = "applied"
        else:
            result["fields"][fname] = "failed"

    # Entity outcomes
    studio = eligible_diff.get("entities", {}).get("studio")
    if studio:
        if mutation_succeeded and "studio_id" in update_fields:
            result["entities"]["studio"] = "applied"
        elif "studio_id" in update_fields:
            result["entities"]["studio"] = "failed"
        else:
            result["entities"]["studio"] = "skipped_resolution"

    performers = eligible_diff.get("entities", {}).get("performers", [])
    if performers:
        if mutation_succeeded and "performer_ids" in update_fields:
            result["entities"]["performers"] = "applied"
        elif "performer_ids" in update_fields:
            result["entities"]["performers"] = "failed"
        else:
            result["entities"]["performers"] = "skipped_resolution"

    return result

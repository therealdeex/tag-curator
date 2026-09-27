// Stash Tag Curator - preview-completeness logic (pure, testable).
//
// Loaded by the manifest BEFORE ui/index.js and exposed as
// window.StashTagCuratorPreviewLogic; ui/index.js consumes it to decide
// what the preview result card may claim.  Kept free of React/DOM so the
// decision logic can be executed directly by tests (tests/ui/
// preview_logic.test.js runs it under Node).
//
// A curate preview produces ONE proposal set per phase (never_processed,
// stale_rules, failed, affected_by_mapping, performer_enrichment).  A
// proposal_detail snapshot therefore covers ALL of the displayed run's
// phase sets or it can prove nothing: rendering a single phase's set and
// finding no changes there must never produce a whole-preview
// "no tag changes" claim.
//
// Statuses returned by previewCompleteness():
//   loading     - snapshot not available (yet).
//   unverified  - the displayed run carries no proposal ids, so identity
//                 cannot be confirmed; nothing conclusive may be shown.
//   stale       - the snapshot does not cover every phase of the displayed
//                 preview (another run's snapshot, or not yet refreshed).
//   legacy      - snapshot predates the multi-phase shape (no `sets` array
//                 or missing per-set totals) - detail unavailable, never a
//                 zero-change claim.
//   no_changes  - every phase accounted for, complete, fully reasoned, and
//                 genuinely zero changes.  The ONLY status on which the UI
//                 may claim "no tag changes".
//   review      - changes are present and/or the detail is incomplete
//                 (truncated page / rows without reason data); render the
//                 rows plus an explicit incompleteness notice.
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.StashTagCuratorPreviewLogic = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Friendly names for the curate phases; unknown phases fall back to
  // their raw name.
  var PHASE_LABELS = {
    never_processed: "new scenes",
    stale_rules: "out-of-date",
    failed: "failed",
    affected_by_mapping: "affected by dictionary edit",
    performer_enrichment: "enrichment",
  };

  function phaseLabel(phase) {
    if (!phase) return null;
    return PHASE_LABELS[phase] || String(phase);
  }

  // Normalize the displayed run's expected proposal sets into
  // [{proposed_run_id, phase}] with non-empty string ids.
  function normalizePhases(phases) {
    var out = [];
    if (!Array.isArray(phases)) return out;
    for (var i = 0; i < phases.length; i++) {
      var entry = phases[i];
      if (!entry) continue;
      var id =
        typeof entry === "string"
          ? entry
          : entry.proposed_run_id;
      if (typeof id === "string" && id) {
        out.push({
          proposed_run_id: id,
          phase:
            typeof entry === "object" && entry.phase ? entry.phase : null,
        });
      }
    }
    return out;
  }

  // Decide what the preview card may claim.  `detail` is the
  // proposal_detail snapshot payload (or null); `phases` is the displayed
  // run's expected proposal sets (proposal_run_ids/phase_proposals from
  // run_history).
  function previewCompleteness(detail, phases) {
    if (!detail) return { status: "loading" };

    var expected = normalizePhases(phases);
    if (expected.length === 0) {
      // No ids to check against: identity is unverified, so the snapshot
      // must not be accepted as "this preview's" outcome.
      return { status: "unverified" };
    }

    var covered = Array.isArray(detail.proposed_run_ids)
      ? detail.proposed_run_ids
      : [];
    for (var i = 0; i < expected.length; i++) {
      if (covered.indexOf(expected[i].proposed_run_id) === -1) {
        // Snapshot does not cover every phase of THIS preview.
        return { status: "stale" };
      }
    }

    var sets = Array.isArray(detail.sets) ? detail.sets : [];
    if (
      sets.length < expected.length ||
      !sets.every(function (s) {
        return (
          s &&
          typeof s.changed_total === "number" &&
          typeof s.without_reasons === "number"
        );
      })
    ) {
      // Older snapshot without the multi-phase shape, or with missing
      // per-set totals: detail unavailable - never a zero-change claim.
      return { status: "legacy" };
    }

    var changedTotal = 0;
    var withoutReasons = 0;
    for (var j = 0; j < sets.length; j++) {
      changedTotal += sets[j].changed_total;
      withoutReasons += sets[j].without_reasons;
    }
    var truncated = !!detail.truncated;

    if (changedTotal === 0 && withoutReasons === 0 && !truncated) {
      // Every phase present, complete, fully reasoned, zero changes.
      return {
        status: "no_changes",
        changedTotal: 0,
        withoutReasons: 0,
        truncated: false,
      };
    }
    return {
      status: "review",
      changedTotal: changedTotal,
      withoutReasons: withoutReasons,
      truncated: truncated,
    };
  }

  return {
    phaseLabel: phaseLabel,
    normalizePhases: normalizePhases,
    previewCompleteness: previewCompleteness,
    PHASE_LABELS: PHASE_LABELS,
  };
});

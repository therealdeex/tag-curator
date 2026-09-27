// Executable tests for the preview-completeness decision logic
// (ui/preview-logic.js).  Run directly under Node:
//
//   node tests/ui/preview_logic.test.js
//
// These tests EXECUTE the logic the preview result card uses (not just
// backend fields or syntax): every case below is one the review found
// could previously produce a false "no tag changes" claim.
"use strict";

const logic = require("../../ui/preview-logic.js");

let failures = 0;
function check(name, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.error(`FAIL ${name}\n  expected: ${e}\n  actual:   ${a}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

// ---------------------------------------------------------------------------
// 1. Multi-phase preview: rebuild phase proposes a removal, final enrichment
//    phase has none.  Both phases covered -> the removal must be shown as a
//    change (status "review"), never a whole-preview zero-change claim.
// ---------------------------------------------------------------------------
const rebuild = {
  proposed_run_id: "prop-a",
  phase: "never_processed",
  total_proposals: 10,
  changed_total: 1,
  without_reasons: 0,
  truncated: false,
};
const enrichment = {
  proposed_run_id: "prop-b",
  phase: "performer_enrichment",
  total_proposals: 10,
  changed_total: 0,
  without_reasons: 0,
  truncated: false,
};
check(
  "multi-phase: removal in rebuild phase is not hidden by unchanged enrichment phase",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a", "prop-b"],
      sets: [rebuild, enrichment],
      proposals: [
        {
          scene_id: 5,
          phase: "never_processed",
          has_change: true,
          added_by_curator: [],
          removed_managed: ["ACT: Old"],
        },
      ],
      changed_total: 1,
      without_reasons: 0,
      truncated: false,
    },
    [
      { proposed_run_id: "prop-a", phase: "never_processed" },
      { proposed_run_id: "prop-b", phase: "performer_enrichment" },
    ]
  ),
  { status: "review", changedTotal: 1, withoutReasons: 0, truncated: false }
);

// ---------------------------------------------------------------------------
// 2. All phases present and genuinely unchanged -> the ONLY zero-change
//    claim the UI may make.
// ---------------------------------------------------------------------------
check(
  "all phases present + genuinely unchanged -> no_changes",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a", "prop-b"],
      sets: [
        { ...rebuild, changed_total: 0 },
        enrichment,
      ],
      proposals: [],
      changed_total: 0,
      without_reasons: 0,
      truncated: false,
    },
    [
      { proposed_run_id: "prop-a", phase: "never_processed" },
      { proposed_run_id: "prop-b", phase: "performer_enrichment" },
    ]
  ),
  {
    status: "no_changes",
    changedTotal: 0,
    withoutReasons: 0,
    truncated: false,
  }
);

// ---------------------------------------------------------------------------
// 3. A phase with ZERO proposals is accounted for: expected ids include it,
//    its section exists with zero counts, and it does not turn a change in
//    another phase into a no-change claim (nor vice versa).
// ---------------------------------------------------------------------------
check(
  "zero-proposal phase accounted for; does not mask another phase's change",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a", "prop-empty"],
      sets: [rebuild, { ...enrichment, proposed_run_id: "prop-empty", total_proposals: 0 }],
      proposals: [
        {
          scene_id: 5,
          phase: "never_processed",
          has_change: true,
          added_by_curator: [],
          removed_managed: ["ACT: Old"],
        },
      ],
      changed_total: 1,
      without_reasons: 0,
      truncated: false,
    },
    [
      { proposed_run_id: "prop-a", phase: "never_processed" },
      { proposed_run_id: "prop-empty", phase: "performer_enrichment" },
    ]
  ),
  { status: "review", changedTotal: 1, withoutReasons: 0, truncated: false }
);
check(
  "zero-proposal phase + all others unchanged -> no_changes is claimable",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a", "prop-empty"],
      sets: [
        { ...rebuild, changed_total: 0 },
        { ...enrichment, proposed_run_id: "prop-empty", total_proposals: 0 },
      ],
      proposals: [],
      changed_total: 0,
      without_reasons: 0,
      truncated: false,
    },
    [
      { proposed_run_id: "prop-a", phase: "never_processed" },
      { proposed_run_id: "prop-empty", phase: "performer_enrichment" },
    ]
  ),
  {
    status: "no_changes",
    changedTotal: 0,
    withoutReasons: 0,
    truncated: false,
  }
);

// ---------------------------------------------------------------------------
// 4. Legacy snapshot (no `sets` shape) carrying additions must NOT yield a
//    zero-change claim -- missing totals mean unavailable, not zero.
// ---------------------------------------------------------------------------
check(
  "legacy snapshot without sets -> legacy (no zero-change claim)",
  logic.previewCompleteness(
    {
      proposed_run_id: "prop-a",
      proposed_run_ids: ["prop-a"],
      proposals: [
        {
          scene_id: 1,
          has_change: true,
          added_by_curator: ["ACT: Blowjob"],
          removed_managed: [],
        },
      ],
    },
    [{ proposed_run_id: "prop-a", phase: null }]
  ),
  { status: "legacy" }
);

// ---------------------------------------------------------------------------
// 5. Missing expected proposal ids -> unverified identity, never acceptance.
// ---------------------------------------------------------------------------
check(
  "missing expected phase ids -> unverified",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a"],
      sets: [{ ...rebuild, changed_total: 0 }],
      proposals: [],
      changed_total: 0,
      without_reasons: 0,
      truncated: false,
    },
    []
  ),
  { status: "unverified" }
);

// ---------------------------------------------------------------------------
// 6. Snapshot belongs to another preview (ids not covered) -> stale.
// ---------------------------------------------------------------------------
check(
  "snapshot from another preview -> stale",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-other"],
      sets: [{ ...rebuild, changed_total: 0, proposed_run_id: "prop-other" }],
      proposals: [],
      changed_total: 0,
      without_reasons: 0,
      truncated: false,
    },
    [{ proposed_run_id: "prop-a", phase: "never_processed" }]
  ),
  { status: "stale" }
);

// ---------------------------------------------------------------------------
// 6b. Snapshot covers only SOME phases of the preview -> stale (the core
//     review finding: latest-set-only snapshots must not pass identity).
// ---------------------------------------------------------------------------
check(
  "partial phase coverage -> stale (single-latest-set snapshot rejected)",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-b"],
      sets: [enrichment],
      proposals: [],
      changed_total: 0,
      without_reasons: 0,
      truncated: false,
    },
    [
      { proposed_run_id: "prop-a", phase: "never_processed" },
      { proposed_run_id: "prop-b", phase: "performer_enrichment" },
    ]
  ),
  { status: "stale" }
);

// ---------------------------------------------------------------------------
// 7. 501-scene truncation + missing per-set numbers are never conclusive.
// ---------------------------------------------------------------------------
check(
  "truncated page with changes -> review",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a"],
      sets: [{ ...rebuild, truncated: true }],
      proposals: [{ scene_id: 501, has_change: true }],
      changed_total: 1,
      without_reasons: 0,
      truncated: true,
    },
    [{ proposed_run_id: "prop-a", phase: "never_processed" }]
  ),
  { status: "review", changedTotal: 1, withoutReasons: 0, truncated: true }
);
check(
  "sets present but totals missing (older detail) -> legacy",
  logic.previewCompleteness(
    {
      proposed_run_ids: ["prop-a"],
      sets: [{ proposed_run_id: "prop-a", total_proposals: 3 }],
      proposals: [],
      truncated: false,
    },
    [{ proposed_run_id: "prop-a", phase: "never_processed" }]
  ),
  { status: "legacy" }
);
check(
  "missing snapshot -> loading",
  logic.previewCompleteness(null, [
    { proposed_run_id: "prop-a", phase: "never_processed" },
  ]),
  { status: "loading" }
);

// ---------------------------------------------------------------------------
// Phase labels (rendering helper).
// ---------------------------------------------------------------------------
check("phaseLabel known phase", logic.phaseLabel("never_processed"), "new scenes");
check("phaseLabel unknown phase falls back", logic.phaseLabel("other"), "other");
check("phaseLabel null phase", logic.phaseLabel(null), null);

if (failures > 0) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nall preview-completeness logic tests passed");

// Stash Tag Curator - UI route.
//
// Registers `/plugin/stash-tag-curator` through Stash's experimental
// PluginApi route surface and renders the curator dashboard:
//
//   Home        - status, one primary action (Update Library), attention
//                 list, result card with per-run change review.
//   Dictionary  - every provider tag and its translation; the place where
//                 mapping decisions are made (triage, edit, batch).
//
// The dashboard orchestrates the full maintenance loop from the browser:
// Scan -> Generate -> Update Library. Waiting on Stash's serial job queue
// is ONLY safe from the UI (a plugin task that polls a job it queued
// behind itself deadlocks), which is why the sequence lives here.
//
// Design invariants (see docs/security.md):
//   * XSS: no raw-HTML injection APIs anywhere. Every
//     server-derived string (tag names, run ids, errors) is rendered as a
//     React text child or a React-controlled attribute.
//   * No second React: React / ReactDOM / react-bootstrap come from Stash
//     via PluginApi, with plain-DOM fallbacks for critical components.
//   * Secrets never touch localStorage; only the active flow descriptor.
//   * The one destructive operation (Update Library) dispatches with
//     confirmed=true only after the operator has seen the scope summary.

(() => {
  const api = window.PluginApi;
  if (
    !api ||
    !api.React ||
    !api.register ||
    typeof api.register.route !== "function"
  ) {
    console.warn(
      "[stash-tag-curator] PluginApi with React + register.route unavailable; UI not registered."
    );
    return;
  }

  // ------------------------------------------------------------------
  // Constants
  // ------------------------------------------------------------------

  const PLUGIN_ID = "stash-tag-curator";
  const ROUTE_PATH = "/plugin/stash-tag-curator";
  const ASSET_BASE = "/plugin/stash-tag-curator/assets/";
  const POLL_INTERVAL_MS = 1000;

  const TERMINAL_STATUSES = new Set([
    "FINISHED",
    "COMPLETE",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "CANCELED",
    "REMOVED",
  ]);

  function isTerminalStatus(status) {
    return TERMINAL_STATUSES.has(String(status || "").toUpperCase());
  }

  // Plain-language mapping used across Home/Dictionary. Internal
  // dispositions (map/detail/ignore/defer) are never shown to the operator.
  const STATUS_META = {
    needs_decision: { label: "Needs decision", variant: "warning" },
    translated: { label: "Translated", variant: "success" },
    kept: { label: "Kept as-is", variant: "secondary" },
    hidden: { label: "Hidden", variant: "dark" },
    deferred: { label: "Postponed", variant: "info" },
  };

  // Dictionary decision -> backend disposition.
  const STATUS_TO_DISPOSITION = {
    translated: "map",
    kept: "detail",
    hidden: "ignore",
    deferred: "defer",
  };

  // Filter tabs (order = triage order).
  const DICTIONARY_TABS = [
    { key: "needs_decision", label: "Needs decision" },
    { key: "translated", label: "Translated" },
    { key: "kept", label: "Kept as-is" },
    { key: "hidden", label: "Hidden" },
    { key: "deferred", label: "Postponed" },
    { key: "pending", label: "Pending save" },
  ];

  // Friendly axis names for the "new canonical tag" flow. The operator picks
  // a category chip; the AXIS: prefix is never typed by hand.
  const AXIS_META = {
    ACT: "Action",
    BODY: "Body",
    THEME: "Theme",
    KINK: "Kink",
    SET: "Setting",
    WARD: "Wardrobe",
    PROD: "Production",
    CAST: "Cast",
    DEMO: "Demographic",
    AGE: "Age",
    ERA: "Era",
    STUDIO: "Studio",
  };
  const AXIS_ORDER = Object.keys(AXIS_META);

  // Run operation tokens -> plain summaries.
  const OP_LABELS = {
    "curate_library": "Update Library",
    "curate_phase": "Update phase",
    "save_mapping": "Dictionary save",
    "preflight": "Preflight",
    "validate_rules": "Validate rules",
    "refresh_data": "Refresh data",
    "run_detail": "Run detail",
    "dashboard": "Dashboard report",
    "unmapped_tags": "Unmapped-tags report",
    "run_history": "Run-history report",
    "rules_audit": "Rules audit",
    "dictionary": "Dictionary report",
  };

  function opLabel(operation) {
    return OP_LABELS[operation] || String(operation || "").replace(/_/g, " ");
  }

  // ------------------------------------------------------------------
  // GraphQL documents (Stash v0.31.1: runPluginTask returns ID!; args_map
  // is the Map scalar).
  // ------------------------------------------------------------------

  const RUN_PLUGIN_TASK_MUTATION = `
    mutation CuratorRunPluginTask(
      $plugin_id: ID!
      $task_name: String
      $description: String
      $args_map: Map
    ) {
      runPluginTask(
        plugin_id: $plugin_id
        task_name: $task_name
        description: $description
        args_map: $args_map
      )
    }
  `;

  const FIND_JOB_QUERY = `
    query CuratorFindJob($id: ID!) {
      findJob(input: { id: $id }) {
        id
        status
        description
        progress
        startTime
        endTime
        error
        subTasks
      }
    }
  `;

  const STOP_JOB_MUTATION = `
    mutation CuratorStopJob($job_id: ID!) {
      stopJob(job_id: $job_id)
    }
  `;

  const JOB_QUEUE_QUERY = `
    query CuratorJobQueue {
      jobQueue {
        id
        status
        description
        subTasks
      }
    }
  `;

  // Stash's own maintenance jobs, dispatched by the dashboard BEFORE the
  // curator task so new files have fingerprints and previews. The curator
  // plugin process itself never waits on these (serial-queue deadlock);
  // the browser does the waiting.
  const METADATA_SCAN_MUTATION = `
    mutation CuratorMetadataScan($input: ScanMetadataInput!) {
      metadataScan(input: $input)
    }
  `;

  const METADATA_GENERATE_MUTATION = `
    mutation CuratorMetadataGenerate($input: GenerateMetadataInput!) {
      metadataGenerate(input: $input)
    }
  `;

  const PLUGIN_SETTINGS_QUERY = `
    query CuratorPluginSettings {
      configuration {
        plugins
      }
    }
  `;

  // ------------------------------------------------------------------
  // React + Bootstrap shims (borrowed from Stash, DOM fallbacks).
  // ------------------------------------------------------------------

  const React = api.React;
  const h = React.createElement;
  const useState = React.useState;
  const useEffect = React.useEffect;
  const useMemo = React.useMemo;
  const useCallback = React.useCallback;
  const useRef = React.useRef;
  const Fragment = React.Fragment;

  const libs = api.libraries || {};
  const BS = libs.Bootstrap || {};
  const BSTabs = BS.Tabs || "div";
  const BSTab = BS.Tab || "div";
  const BSBadge = BS.Badge || "span";
  const BSButton = BS.Button || "button";
  const BSModal = BS.Modal || null;
  const BSSpinner = BS.Spinner || null;

  function FormControl(props) {
    const C = (BS.Form && BS.Form.Control) || null;
    if (C && (typeof C === "function" || (typeof C === "object" && C.render))) {
      return h(C, props);
    }
    const domProps = Object.assign({}, props);
    delete domProps.as;
    return h(
      "input",
      Object.assign({ className: "form-control" }, domProps, {
        className: "form-control " + (domProps.className || ""),
      })
    );
  }

  function Spinner(props) {
    if (BSSpinner && (typeof BSSpinner === "function" || BSSpinner.render)) {
      return h(BSSpinner, Object.assign({ size: "sm" }, props));
    }
    return h("span", { className: "stash-tag-curator-spinner", "aria-hidden": true });
  }

  // ------------------------------------------------------------------
  // Transport: one GraphQL helper + one JSON snapshot fetcher.
  // ------------------------------------------------------------------

  async function gql(query, variables) {
    let resp;
    try {
      resp = await fetch("/graphql", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query, variables: variables || {} }),
      });
    } catch (networkErr) {
      throw new Error("network error: " + networkErr.message);
    }
    if (resp.status === 401 || resp.status === 403) {
      throw new Error("not authenticated (HTTP " + resp.status + ")");
    }
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status + " from /graphql");
    }
    let body;
    try {
      body = await resp.json();
    } catch (parseErr) {
      throw new Error("invalid JSON from /graphql");
    }
    if (body.errors && body.errors.length) {
      throw new Error(body.errors[0].message || "GraphQL error");
    }
    return body.data;
  }

  async function fetchSnapshot(name) {
    const url = ASSET_BASE + name + ".json?_=" + Date.now();
    const resp = await fetch(url, { credentials: "same-origin" });
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status + " for " + name);
    }
    return resp.json();
  }

  // ------------------------------------------------------------------
  // localStorage (never secrets - only the active flow descriptor and the
  // pending dictionary-edit draft + last-save outcome).
  // ------------------------------------------------------------------

  const LS_ACTIVE_FLOW = "stashTagCurator.activeFlow";
  const LS_PENDING_EDITS = "stashTagCurator.pendingEdits";
  const LS_LAST_SAVE = "stashTagCurator.lastSave";

  function lsGet(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) return fallback;
      return JSON.parse(raw);
    } catch (err) {
      return fallback;
    }
  }

  function lsSet(key, value) {
    try {
      if (value === null || value === undefined) {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, JSON.stringify(value));
      }
    } catch (err) {
      // Private mode / quota: the UI still works, it just forgets drafts.
    }
  }

  // ------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------

  function cx() {
    const parts = [];
    for (let i = 0; i < arguments.length; i++) {
      const v = arguments[i];
      if (v) parts.push(v);
    }
    return parts.join(" ");
  }

  function formatTimestamp(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }

  function relativeTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const seconds = Math.round((Date.now() - d.getTime()) / 1000);
    if (seconds < 90) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 90) return minutes + " min ago";
    const hours = Math.round(minutes / 60);
    if (hours < 36) return hours + " h ago";
    const days = Math.round(hours / 24);
    return days + " d ago";
  }

  function splitAxis(canonicalName) {
    const idx = String(canonicalName || "").indexOf(": ");
    if (idx <= 0) return { axis: null, label: String(canonicalName || "") };
    return {
      axis: String(canonicalName).slice(0, idx),
      label: String(canonicalName).slice(idx + 2),
    };
  }

  // ------------------------------------------------------------------
  // Asset snapshot hook (one implementation for every panel).
  // ------------------------------------------------------------------

  function useAssetSnapshot(name, opts) {
    const options = opts || {};
    const refreshKey = options.refreshKey;
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const aliveRef = useRef(true);

    const fetchOnce = useCallback(() => {
      fetchSnapshot(name)
        .then((payload) => {
          if (!aliveRef.current) return;
          setData(payload);
          setError(null);
          setLoading(false);
        })
        .catch((err) => {
          if (!aliveRef.current) return;
          setError(err && err.message ? err.message : String(err));
          setLoading(false);
        });
    }, [name]);

    useEffect(() => {
      aliveRef.current = true;
      setLoading(true);
      fetchOnce();
      return () => {
        aliveRef.current = false;
      };
    }, [fetchOnce, refreshKey]);

    return { data: data, error: error, loading: loading, refresh: fetchOnce };
  }

  // ------------------------------------------------------------------
  // The one destructive operation: Update Library (mode curate_library).
  // `preview` runs the same pipeline without writing anything.
  // ------------------------------------------------------------------

  const CURATE_TASK_NAME = "Update Library";
  const PREVIEW_TASK_NAME = "Preview Update Library";

  // ------------------------------------------------------------------
  // Modal shell: react-bootstrap Modal when present, styled overlay
  // fallback otherwise.
  // ------------------------------------------------------------------

  function ModalShell(props) {
    const title = props.title;
    const onClose = props.onClose;
    const children = props.children;
    const footer = props.footer;

    const body = h(
      "div",
      { className: "stash-tag-curator-modal-body" },
      children,
      h(
        "div",
        { className: "stash-tag-curator-modal-footer" },
        footer
      )
    );

    if (BSModal && typeof BSModal === "function") {
      return h(
        BSModal,
        {
          show: true,
          onHide: onClose,
          centered: true,
          dialogClassName: "stash-tag-curator-modal-dialog",
        },
        h("div", { className: "modal-content" },
          h("div", { className: "modal-header" },
            h("h5", { className: "modal-title" }, title),
            h("button", {
              type: "button",
              className: "btn-close",
              "aria-label": "Close",
              onClick: onClose,
            })
          ),
          h("div", { className: "modal-body" }, body)
        )
      );
    }

    // Fallback overlay.
    return h(
      "div",
      {
        className: "stash-tag-curator-modal-overlay",
        role: "dialog",
        "aria-modal": "true",
        "aria-label": typeof title === "string" ? title : "Dialog",
      },
      h(
        "div",
        { className: "stash-tag-curator-modal-fallback" },
        h(
          "div",
          { className: "stash-tag-curator-modal-header" },
          h("h5", null, title),
          h(
            "button",
            { type: "button", className: "btn btn-sm btn-outline-secondary", onClick: onClose, "aria-label": "Close" },
            "X"
          )
        ),
        body
      )
    );
  }

  // ------------------------------------------------------------------
  // useFlow: sequential job runner. Every dashboard action is a short
  // FLOW of jobs:
  //
  //   Update Library  -> [scan?] -> [generate?] -> curator task
  //   Preview         -> curator task (preview=true)
  //   Dictionary save -> curator task (Save Dictionary Edit)
  //   Refresh data    -> curator task (Refresh Data)
  //
  // The flow descriptor (steps + current job id) is persisted to
  // localStorage so an in-flight sequence survives a page reload. Waiting
  // on Stash jobs is safe ONLY here in the browser: a plugin task that
  // polls a job queued behind itself deadlocks on Stash's serial queue.
  // ------------------------------------------------------------------

  function useFlow() {
    const [flow, setFlow] = useState(null); // {steps:[{kind,label}], idx}
    const [job, setJob] = useState(null); // current job state
    const [error, setError] = useState(null);
    const [done, setDone] = useState(null); // {flow, failed}
    const timerRef = useRef(null);
    const jobRef = useRef(null);
    const flowRef = useRef(null);
    const onCompleteRef = useRef(null);

    const clearTimer = useCallback(() => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }, []);

    const persist = useCallback((f, j) => {
      if (!f) {
        lsSet(LS_ACTIVE_FLOW, null);
        return;
      }
      lsSet(LS_ACTIVE_FLOW, {
        steps: f.steps,
        idx: f.idx,
        job_id: j ? j.job_id : null,
      });
    }, []);

    const finish = useCallback((failed) => {
      clearTimer();
      const f = flowRef.current;
      flowRef.current = null;
      jobRef.current = null;
      setFlow(null);
      setJob(null);
      lsSet(LS_ACTIVE_FLOW, null);
      if (f) setDone({ steps: f.steps, failed: !!failed });
      if (onCompleteRef.current) {
        const cb = onCompleteRef.current;
        onCompleteRef.current = null;
        cb(failed);
      }
    }, [clearTimer]);

    const pollOnce = useCallback(async (jobId) => {
      try {
        const data = await gql(FIND_JOB_QUERY, { id: jobId });
        const j = (data && data.findJob) || null;
        if (!j) {
          // Job evicted from the queue -> treat as finished.
          const f = flowRef.current;
          const isLast = !f || f.idx >= f.steps.length - 1;
          if (isLast) finish(false);
          else advance();
          return;
        }
        const updated = {
          job_id: jobId,
          status: j.status,
          progress: typeof j.progress === "number" ? j.progress : 0,
          error: j.error || null,
          sub_tasks: j.subTasks || null,
          description: j.description || null,
        };
        setJob(updated);
        if (isTerminalStatus(j.status)) {
          const ok =
            j.status === "FINISHED" ||
            j.status === "COMPLETE" ||
            j.status === "COMPLETED";
          const f = flowRef.current;
          const isLast = !f || f.idx >= f.steps.length - 1;
          if (!ok) {
            finish(true);
            setError(
              "Step failed (" + (f && f.steps[f.idx] ? f.steps[f.idx].label : "job") +
              "): " + (j.error || j.status)
            );
          } else if (isLast) {
            finish(false);
          } else {
            advance();
          }
          return;
        }
        timerRef.current = setTimeout(() => pollOnce(jobId), POLL_INTERVAL_MS);
      } catch (err) {
        setError("status check failed: " + (err && err.message ? err.message : String(err)));
        timerRef.current = setTimeout(() => pollOnce(jobId), POLL_INTERVAL_MS * 2);
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [finish]);

    // Advance to the next flow step.
    function advance() {
      const f = flowRef.current;
      if (!f) {
        finish(false);
        return;
      }
      const nextIdx = f.idx + 1;
      if (nextIdx >= f.steps.length) {
        finish(false);
        return;
      }
      const nextFlow = { steps: f.steps, idx: nextIdx };
      flowRef.current = nextFlow;
      setFlow(nextFlow);
      setJob(null);
      dispatchStep(nextFlow);
    }

    // Dispatch the job for flow.idx (curator task or Stash maintenance job).
    async function dispatchStep(f) {
      const step = f.steps[f.idx];
      try {
        let data;
        if (step.kind === "curator") {
          data = await gql(RUN_PLUGIN_TASK_MUTATION, {
            plugin_id: PLUGIN_ID,
            task_name: step.taskName,
            description: step.label,
            args_map: step.argsMap || {},
          });
        } else {
          const mutation =
            step.kind === "scan"
              ? METADATA_SCAN_MUTATION
              : METADATA_GENERATE_MUTATION;
          data = await gql(mutation, { input: step.input || {} });
        }
        const jobId =
          data && (data.runPluginTask || data.metadataScan || data.metadataGenerate);
        if (!jobId) {
          throw new Error(step.label + " returned no job id");
        }
        jobRef.current = jobId;
        setJob({
          job_id: jobId,
          status: "READY",
          progress: 0,
          error: null,
          sub_tasks: null,
        });
        persist(f, { job_id: jobId });
        clearTimer();
        timerRef.current = setTimeout(() => pollOnce(jobId), POLL_INTERVAL_MS);
      } catch (err) {
        setError(
          "failed to start " + step.label + ": " +
            (err && err.message ? err.message : String(err))
        );
        finish(true);
      }
    }

    const start = useCallback((steps, onComplete) => {
      if (flowRef.current) {
        setError("a flow is already running; wait for it to finish or cancel it");
        return;
      }
      setError(null);
      setDone(null);
      const f = { steps: steps, idx: 0 };
      flowRef.current = f;
      onCompleteRef.current = onComplete || null;
      setFlow(f);
      setJob(null);
      persist(f, null);
      dispatchStep(f);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [persist]);

    const cancel = useCallback(() => {
      const current = jobRef.current;
      if (!current) return;
      gql(STOP_JOB_MUTATION, { job_id: current }).catch((err) => {
        setError(
          "cancel failed: " + (err && err.message ? err.message : String(err))
        );
      });
    }, []);

    const dismiss = useCallback(() => {
      clearTimer();
      flowRef.current = null;
      jobRef.current = null;
      onCompleteRef.current = null;
      setFlow(null);
      setJob(null);
      setDone(null);
      setError(null);
      lsSet(LS_ACTIVE_FLOW, null);
    }, [clearTimer]);

    // Resume an in-flight flow after a page reload.
    useEffect(() => {
      const saved = lsGet(LS_ACTIVE_FLOW, null);
      if (saved && saved.steps && saved.steps.length && saved.job_id) {
        const f = { steps: saved.steps, idx: saved.idx || 0 };
        flowRef.current = f;
        jobRef.current = saved.job_id;
        setFlow(f);
        setJob({ job_id: saved.job_id, status: "RUNNING", progress: 0 });
        timerRef.current = setTimeout(
          () => pollOnce(saved.job_id),
          POLL_INTERVAL_MS
        );
      }
      return () => {
        clearTimer();
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const running = !!flowRef.current && !!job &&
      !isTerminalStatus(job.status) && job.status !== "STOPPING";

    return {
      flow: flow,
      job: job,
      error: error,
      done: done,
      running: running,
      start: start,
      cancel: cancel,
      dismiss: dismiss,
    };
  }

  // ------------------------------------------------------------------
  // ConfirmModal: the gate for Update Library. Body lines explain scope
  // in plain language; option checkboxes toggle flow behaviour.
  // ------------------------------------------------------------------

  function ConfirmModal(props) {
    const title = props.title;
    const summary = props.summary;
    const scopeLabel = props.scopeLabel;
    const lines = props.lines || [];
    const options = props.options || [];
    const confirmText = props.confirmText || "Confirm";
    const onClose = props.onClose;
    const onConfirm = props.onConfirm;
    return h(
      ModalShell,
      {
        title: title,
        onClose: onClose,
        footer: [
          h(
            BSButton,
            { key: "cancel", variant: "outline-secondary", onClick: onClose },
            "Cancel"
          ),
          h(
            BSButton,
            { key: "confirm", variant: "primary", onClick: onConfirm },
            confirmText
          ),
        ],
      },
      h("p", { className: "stash-tag-curator-confirm-summary" }, summary),
      scopeLabel
        ? h(
            "p",
            { className: "stash-tag-curator-confirm-scope" },
            "Scope: ",
            h("strong", null, scopeLabel)
          )
        : null,
      options.length
        ? h(
            "div",
            { className: "stash-tag-curator-confirm-options" },
            options.map((opt) =>
              h(
                "label",
                { key: opt.key, className: "stash-tag-curator-confirm-option" },
                h("input", {
                  type: "checkbox",
                  checked: opt.checked,
                  onChange: (e) => opt.onChange(e.target.checked),
                }),
                " ",
                opt.label
              )
            )
          )
        : null,
      lines.length
        ? h(
            "ul",
            { className: "stash-tag-curator-confirm-lines" },
            lines.map((line, i) => h("li", { key: i }, line))
          )
        : null,
      h(
        "p",
        { className: "stash-tag-curator-confirm-undo" },
        "Everything Update Library writes is derived from your providers and " +
          "dictionary — running it again re-derives and corrects it. Each " +
          "run's changes are listed in Recent runs."
      )
    );
  }

  // ------------------------------------------------------------------
  // ProgressPanel: live status of the running flow with cancel/dismiss.
  // Renders the phase breadcrumb (Scan -> Generate -> Update Library),
  // the backend's own subTask string (current phase detail), and the
  // queue position when the job is waiting behind another Stash job.
  // ------------------------------------------------------------------

  function ProgressPanel(props) {
    const flow = props.flow;
    const job = props.job;
    const error = props.error;
    const onCancel = props.onCancel;
    const onDismiss = props.onDismiss;

    if (!flow) {
      return error
        ? h(
            "div",
            { className: "alert alert-danger stash-tag-curator-jobpanel", role: "alert" },
            error,
            " ",
            h(
              BSButton,
              { variant: "link", size: "sm", onClick: onDismiss },
              "dismiss"
            )
          )
        : null;
    }

    const status = String((job && job.status) || "").toUpperCase();
    const failed =
      status === "FAILED" || status === "CANCELLED" || status === "REMOVED";
    const running = !isTerminalStatus(status) && !failed;
    const stepProgress = Math.round((Number(job && job.progress) || 0) * 100);
    const overall =
      ((flow.idx + (job ? Math.min(1, Math.max(0, job.progress || 0)) : 0)) /
        flow.steps.length) *
      100;

    const stepLabel = (flow.steps[flow.idx] || {}).label || "Working";

    return h(
      "div",
      {
        className: cx(
          "stash-tag-curator-jobpanel",
          failed ? "stash-tag-curator-jobpanel-error" : "stash-tag-curator-jobpanel-active"
        ),
      },
      h(
        "div",
        { className: "d-flex align-items-center justify-content-between gap-2" },
        h(
          "div",
          null,
          h(
            "span",
            { className: "stash-tag-curator-flow-steps" },
            flow.steps.map((s, i) =>
              h(
                "span",
                {
                  key: i,
                  className: cx(
                    "stash-tag-curator-flow-step",
                    i < flow.idx && "stash-tag-curator-flow-step-done",
                    i === flow.idx && "stash-tag-curator-flow-step-active"
                  ),
                },
                i > 0 ? h("span", { className: "stash-tag-curator-flow-sep" }, " → ") : null,
                s.label
              )
            )
          ),
          " ",
          h(BSBadge, { bg: failed ? "danger" : running ? "primary" : "success" },
            running ? stepProgress + "%" : status.toLowerCase())
        ),
        h(
          "div",
          { className: "d-flex gap-2" },
          running
            ? h(BSButton, { variant: "outline-danger", size: "sm", onClick: onCancel }, "Cancel")
            : h(BSButton, { variant: "outline-secondary", size: "sm", onClick: onDismiss }, "Dismiss")
        )
      ),
      h(
        "div",
        {
          className: "stash-tag-curator-progress",
          role: "progressbar",
          "aria-valuemin": 0,
          "aria-valuemax": 100,
          "aria-valuenow": Math.round(overall),
          "aria-label": "Overall progress",
        },
        h("div", {
          className: "stash-tag-curator-progress-fill" + (failed ? " stash-tag-curator-progress-error" : ""),
          style: { width: overall + "%" },
        })
      ),
      running && job && job.sub_tasks && job.sub_tasks.length
        ? h(
            "div",
            { className: "stash-tag-curator-muted small" },
            h(Spinner),
            " ",
            String(job.sub_tasks[job.sub_tasks.length - 1])
          )
        : null,
      running && status === "READY"
        ? h(
            "div",
            { className: "stash-tag-curator-muted small" },
            h(Spinner),
            " Waiting for Stash's job queue (one job runs at a time)."
          )
        : null,
      error
        ? h("div", { className: "stash-tag-curator-error-text" }, String(error))
        : null,
      job && job.error
        ? h("div", { className: "stash-tag-curator-error-text" }, String(job.error))
        : null
    );
  }

  // ------------------------------------------------------------------
  // Shared snapshot-refresh button.
  // ------------------------------------------------------------------

  function RefreshButton(props) {
    const onClick = props.onClick;
    const busy = props.busy;
    return h(
      BSButton,
      {
        variant: "outline-secondary",
        size: "sm",
        onClick: onClick,
        disabled: !!busy,
        "aria-label": "Refresh data",
      },
      busy ? "Refreshing…" : "Refresh"
    );
  }

  // ------------------------------------------------------------------
  // ResultCard: what a finished run actually did, with a per-scene diff.
  // Backed by the run_history + run_detail snapshots, which the plugin
  // regenerates as soon as a run completes.
  // ------------------------------------------------------------------

  function summarizeRunCounts(run) {
    const parts = [];
    if (run.scenes_changed > 0) parts.push(run.scenes_changed + " updated");
    if (run.scenes_ok > 0) parts.push(run.scenes_ok + " already correct");
    if (run.tags_deleted > 0) parts.push(run.tags_deleted + " unused tags removed");
    if (run.failures > 0) parts.push(run.failures + " failed");
    if (run.unmapped_count > 0) parts.push(run.unmapped_count + " unmapped tags seen");
    if (!parts.length) parts.push("no changes needed");
    return parts.join(" · ");
  }

  function ResultCard(props) {
    const run = props.run; // run_history entry
    const detail = props.detail; // run_detail snapshot payload
    const needsDecision = props.needsDecision;
    const onDismiss = props.onDismiss;
    const onGotoDictionary = props.onGotoDictionary;
    const preview = !!props.preview;

    const [showChanges, setShowChanges] = useState(false);
    if (!run) return null;

    const changes = (detail && detail.changes) || [];
    const status = String(run.status || "");
    const failedRun = status === "failed";

    return h(
      "div",
      {
        className: cx(
          "stash-tag-curator-result",
          failedRun ? "stash-tag-curator-result-error" : "stash-tag-curator-result-ok"
        ),
      },
      h(
        "div",
        { className: "stash-tag-curator-result-head" },
        h(
          "strong",
          null,
          preview ? "Preview complete — nothing was written" :
            failedRun ? "Update Library failed" : "Update Library complete"
        ),
        h("span", { className: "stash-tag-curator-muted" },
          " " + relativeTime(run.ended_at || run.started_at)),
        h(
          "div",
          { className: "stash-tag-curator-result-actions" },
          h(
            BSButton,
            {
              size: "sm",
              variant: "link",
              onClick: () => setShowChanges((v) => !v),
            },
            showChanges ? "Hide changes" : "Review changes"
          ),
          h(BSButton, { size: "sm", variant: "link", onClick: onDismiss }, "Dismiss")
        )
      ),
      failedRun && run.error
        ? h("div", { className: "stash-tag-curator-error-text" }, String(run.error))
        : null,
      h(
        "div",
        { className: "stash-tag-curator-result-summary" },
        preview
          ? (run.proposals_written || 0) + " scene" +
            ((run.proposals_written || 0) === 1 ? "" : "s") +
            " would change."
          : summarizeRunCounts(run)
      ),
      !preview && needsDecision !== null && needsDecision > 0
        ? h(
            "div",
            { className: "stash-tag-curator-result-hint" },
            h(
              BSButton,
              { variant: "link", size: "sm", onClick: onGotoDictionary },
              needsDecision + " provider tag" +
                (needsDecision === 1 ? "" : "s") +
                " need a dictionary decision"
            )
          )
        : null,
      showChanges
        ? h(
            "div",
            { className: "stash-tag-curator-diff" },
            detail && detail.total_changes === 0
              ? h(
                  "div",
                  { className: "stash-tag-curator-muted" },
                  "This run made no scene changes."
                )
              : null,
            detail && detail.changes_without_names > 0
              ? h(
                  "div",
                  { className: "stash-tag-curator-muted small" },
                  detail.changes_without_names +
                    " older change(s) predate the diff record."
                )
              : null,
            (changes || []).slice(0, 50).map((c) =>
              h(
                "div",
                { key: c.scene_id, className: "stash-tag-curator-diff-row" },
                h(
                  "span",
                  { className: "stash-tag-curator-diff-scene" },
                  "Scene " + c.scene_id
                ),
                (c.removed_tags || []).map((t) =>
                  h(
                    "span",
                    { key: "-" + t, className: "stash-tag-curator-chip-diff stash-tag-curator-chip-removed" },
                    "− " + t
                  )
                ),
                (c.added_tags || []).map((t) =>
                  h(
                    "span",
                    { key: "+" + t, className: "stash-tag-curator-chip-diff stash-tag-curator-chip-added" },
                    "+ " + t
                  )
                ),
                !(c.added_tags || []).length && !(c.removed_tags || []).length
                  ? h(
                      "span",
                      { className: "stash-tag-curator-muted" },
                      "metadata only"
                    )
                  : null
              )
            ),
            detail && detail.total_changes > 50
              ? h(
                  "div",
                  { className: "stash-tag-curator-muted small" },
                  "Showing the first 50 of " + detail.total_changes + " changed scenes."
                )
              : null,
            !detail
              ? h(
                  "div",
                  { className: "stash-tag-curator-muted" },
                  "Change details load after the run finishes…"
                )
              : null
          )
        : null
    );
  }

  // ------------------------------------------------------------------
  // HomePanel: status hero, one primary action, attention list, recent
  // runs, and a collapsed details section.
  // ------------------------------------------------------------------

  function HomePanel(props) {
    const flowState = props.flowState;
    const dashboard = props.dashboard;
    const dictionary = props.dictionary;
    const runHistory = props.runHistory;
    const lastSave = props.lastSave;
    const onRunUpdate = props.onRunUpdate;
    const onRunPreview = props.onRunPreview;
    const onGoto = props.onGoto;
    const onApplyEdits = props.onApplyEdits;
    const onDismissSave = props.onDismissSave;
    const onRunOp = props.onRunOp;
    const refreshing = props.refreshing;
    const busy = props.busy;

    const d = dashboard.data || {};
    const totals = d.totals || {};
    const lock = d.active_job || null;

    const needsDecision = dictionary.data
      ? (dictionary.data.stats || {}).needs_decision
      : null;
    const totalScenes = totals.total_scenes;
    const processed = totals.processed;
    const upToDate =
      totalScenes > 0 && processed === totalScenes && (totals.failed || 0) === 0;

    const runs = ((runHistory.data && runHistory.data.runs) || []).filter(
      (r) => !r.parent_run_id
    );
    const lastRun = runs[0] || null;

    // Attention items, highest priority first.
    const attention = [];
    if (lock && lock.run_id) {
      const hb = lock.heartbeat_ts ? new Date(lock.heartbeat_ts).getTime() : 0;
      const stale = !hb || Date.now() - hb > 120000;
      if (stale) {
        attention.push({
          key: "interrupted",
          tone: "warning",
          text:
            "A previous " + opLabel(lock.operation) +
            " run was interrupted. Nothing is lost — the next Update Library " +
            "continues where it left off.",
          actions: [{ label: "Continue now", run: "_update" }],
        });
      } else {
        attention.push({
          key: "running",
          tone: "info",
          text:
            "A " + opLabel(lock.operation) + " run is in progress" +
            (lock.started_at ? " (started " + relativeTime(lock.started_at) + ")" : "") +
            ". The dashboard picks it up when it finishes.",
          actions: null,
        });
      }
    }
    if (lastSave && !lastSave.applied && lastSave.affected_scene_count > 0) {
      attention.push({
        key: "apply",
        tone: "warning",
        text:
          "Your last dictionary edit (" + lastSave.edit_count +
          " tag" + (lastSave.edit_count === 1 ? "" : "s") + ", saved " +
          relativeTime(lastSave.at) + ") affects " +
          lastSave.affected_scene_count + " scene" +
          (lastSave.affected_scene_count === 1 ? "" : "s") + ".",
        actions: [{ label: "Update those scenes now", run: "_apply_edits" }],
        dismiss: { label: "Later", run: "_dismiss_save" },
      });
    }
    if (needsDecision !== null && needsDecision > 0) {
      attention.push({
        key: "decisions",
        tone: "info",
        text:
          needsDecision + " provider tag" + (needsDecision === 1 ? " needs" : "s need") +
          " a decision in your dictionary.",
        actions: [{ label: "Review dictionary", run: "_goto_dictionary" }],
      });
    }
    if ((totals.failed || 0) > 0) {
      attention.push({
        key: "failed",
        tone: "warning",
        text:
          totals.failed + " scene" + (totals.failed === 1 ? "" : "s") +
          " failed last time. The next Update Library retries them automatically.",
        actions: null,
      });
    }

    const recentErrors = d.recent_errors || [];
    const errorGroups = {};
    (recentErrors || []).forEach((e) => {
      const msg = String((e && (e.error || e.message)) || "");
      if (!msg) return;
      errorGroups[msg] = (errorGroups[msg] || 0) + 1;
    });
    const groupedErrors = Object.keys(errorGroups).map((msg) => ({
      message: msg,
      count: errorGroups[msg],
    }));

    return h(
      "div",
      { className: "stash-tag-curator-home" },
      h(
        "div",
        { className: "stash-tag-curator-hero" },
        h(
          "div",
          { className: "stash-tag-curator-hero-status" },
          h(
            "div",
            { className: "stash-tag-curator-hero-title" },
            upToDate
              ? "Library is curated"
              : totalScenes > 0
                ? "Library needs attention"
                : "No scenes processed yet",
            h(
              "div",
              { className: "stash-tag-curator-hero-sub" },
              totalScenes !== undefined
                ? totalScenes + " scenes · " + (processed || 0) + " processed" +
                  (lastRun ? " · last run " + relativeTime(lastRun.started_at) : "")
                : (dashboard.loading ? "loading…" : "no data yet")
            )
          ),
          h(
            "div",
            { className: "d-flex gap-2" },
            h(
              BSButton,
              {
                variant: "primary",
                size: "lg",
                disabled: !dashboard.data || busy,
                onClick: onRunUpdate,
              },
              "Update Library"
            ),
            h(
              BSButton,
              {
                variant: "outline-secondary",
                size: "lg",
                disabled: !dashboard.data || busy,
                onClick: onRunPreview,
              },
              "Preview"
            )
          )
        )
      ),
      attention.length
        ? h(
            "div",
            { className: "stash-tag-curator-attention" },
            attention.map((item) =>
              h(
                "div",
                {
                  key: item.key,
                  className: "stash-tag-curator-attention-item stash-tag-curator-tone-" + item.tone,
                },
                h("div", { className: "stash-tag-curator-attention-text" }, item.text),
                h(
                  "div",
                  { className: "stash-tag-curator-attention-actions" },
                  (item.actions || []).map((a) =>
                    h(
                      BSButton,
                      {
                        key: a.run,
                        size: "sm",
                        variant: item.tone === "danger" ? "danger" : "outline-primary",
                        onClick: () => {
                          if (a.run === "_goto_dictionary") onGoto("dictionary");
                          else if (a.run === "_apply_edits") onApplyEdits();
                          else if (a.run === "_update") onRunUpdate();
                        },
                      },
                      a.label
                    )
                  ),
                  item.dismiss
                    ? h(
                        BSButton,
                        {
                          size: "sm",
                          variant: "link",
                          onClick: () => onDismissSave(),
                        },
                        item.dismiss.label
                      )
                    : null
                )
              )
            )
          )
        : h(
            "div",
            { className: "stash-tag-curator-attention-item stash-tag-curator-tone-ok" },
            h("div", { className: "stash-tag-curator-attention-text" },
              "Nothing needs your attention right now.")
          ),
      h(
        "div",
        { className: "stash-tag-curator-section" },
        h(
          "div",
          { className: "stash-tag-curator-section-head" },
          h("h6", null, "Recent runs"),
          h(RefreshButton, { onClick: props.onRefresh, busy: refreshing })
        ),
        runs.length
          ? runs.slice(0, 5).map((run) =>
              h(
                "div",
                {
                  key: run.run_id,
                  className: "stash-tag-curator-runrow",
                  onClick: () => props.onSelectRun(run),
                },
                h("strong", null, opLabel(run.operation)),
                run.scope && run.scope !== "maintenance" && run.scope !== "null"
                  ? h("span", { className: "stash-tag-curator-muted" }, " · " + run.scope.replace(/_/g, " "))
                  : null,
                h("span", { className: "stash-tag-curator-runrow-when" },
                  relativeTime(run.started_at)),
                h(BSBadge, { bg: badgeVariantForStatus(run.status) },
                  String(run.status || "").toLowerCase()),
                h(
                  "span",
                  { className: "stash-tag-curator-runrow-counts" },
                  summarizeRunCounts(run)
                )
              )
            )
          : h(
              "div",
              { className: "stash-tag-curator-empty" },
              "No runs yet. Update Library will appear here."
            )
      ),
      groupedErrors.length
        ? h(
            "div",
            { className: "stash-tag-curator-section" },
            h("h6", null, "Notes from the last run"),
            h(
              "ul",
              { className: "stash-tag-curator-error-list" },
              groupedErrors.map((g) =>
                h(
                  "li",
                  { key: g.message },
                  g.message,
                  g.count > 1 ? h("span", { className: "stash-tag-curator-muted" }, "  ×" + g.count) : null
                )
              )
            )
          )
        : null,
      h(
        "details",
        { className: "stash-tag-curator-details" },
        h("summary", null, "Details and maintenance"),
        h(
          "div",
          { className: "stash-tag-curator-stats" },
          h(StatCard, { value: totals.total_scenes, label: "Total scenes" }),
          h(StatCard, { value: totals.processed, label: "Processed" }),
          h(StatCard, { value: totals.never_processed, label: "Never processed" }),
          h(StatCard, { value: totals.stale, label: "Out of date" }),
          h(StatCard, { value: totals.failed, label: "Failed" }),
          h(StatCard, {
            value: needsDecision === null ? undefined : needsDecision,
            label: "Tags needing decision",
          })
        ),
        h(
          "div",
          { className: "stash-tag-curator-dl-row" },
          h(
            "dl",
            { className: "stash-tag-curator-dl" },
            h("dt", null, "Dictionary version"),
            h("dd", null, (d.rules && d.rules.version) || "-"),
            h("dt", null, "Checksum"),
            h("dd", { className: "stash-tag-curator-mono" }, String((d.rules && d.rules.checksum) || "").slice(0, 16) + "…"),
            h("dt", null, "Providers"),
            h("dd", null, (d.configured_providers || []).join(", ") || "-")
          ),
          h(
            "div",
            { className: "stash-tag-curator-opgrid" },
            h(BSButton, { variant: "outline-secondary", size: "sm", onClick: () => onRunOp("preflight") }, "Preflight"),
            h(BSButton, { variant: "outline-secondary", size: "sm", onClick: () => onRunOp("validate") }, "Validate dictionary")
          )
        ),
        h(
          "p",
          { className: "stash-tag-curator-muted small" },
          "Data from " + formatTimestamp(d.generated_at) +
            (d.generated_at ? " (" + relativeTime(d.generated_at) + ")" : "")
        )
      )
    );
  }

  function badgeVariantForStatus(status) {
    const s = String(status || "").toLowerCase();
    if (s === "completed" || s === "success") return "success";
    if (s === "failed") return "danger";
    if (s === "abandoned" || s === "interrupted") return "warning";
    if (s === "running") return "primary";
    return "secondary";
  }

  function StatCard(props) {
    return h(
      "div",
      { className: "stash-tag-curator-stat" },
      h("div", { className: "stash-tag-curator-stat-value" }, props.value === undefined ? "-" : String(props.value)),
      h("div", { className: "stash-tag-curator-stat-label" }, props.label)
    );
  }

  // ------------------------------------------------------------------
  // CanonicalTypeahead: pick existing canonical tags or create a new one.
  // Suggestions (from the backend's fuzzy pass and the operator's query)
  // are plain lists; the AXIS: prefix is never typed by hand.
  // ------------------------------------------------------------------

  function CanonicalTypeahead(props) {
    const canonical = props.canonical || [];
    const query = props.query || "";
    const onQuery = props.onQuery;
    const selected = props.selected || [];
    const onSelect = props.onSelect;
    const onRemove = props.onRemove;
    const suggestions = props.suggestions || [];
    const inputRef = props.inputRef || null;

    const [newAxis, setNewAxis] = useState("THEME");

    const q = query.trim().toLowerCase();
    const exact = q ? canonical.find((c) => c.name.toLowerCase() === q) : null;
    const matches = q
      ? canonical
          .filter((c) => c.name.toLowerCase().indexOf(q) >= 0 && c.name !== (exact && exact.name))
          .slice(0, 8)
      : [];
    const canCreate = q && !exact;

    function pick(name) {
      onSelect(name);
      onQuery("");
    }

    return h(
      "div",
      { className: "stash-tag-curator-typeahead" },
      selected.length
        ? h(
            "div",
            { className: "stash-tag-curator-chip-row" },
            selected.map((name) =>
              h(
                "span",
                { key: name, className: "stash-tag-curator-chip" },
                name,
                " ",
                h(
                  "button",
                  {
                    type: "button",
                    className: "stash-tag-curator-chip-x",
                    "aria-label": "Remove " + name,
                    onClick: () => onRemove(name),
                  },
                  "×"
                )
              )
            )
          )
        : null,
      h(FormControl, {
        type: "text",
        value: query,
        placeholder: "Search your tags…",
        "aria-label": "Search canonical tags",
        ref: inputRef,
        onChange: (e) => onQuery(e.target.value),
        onKeyDown: (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.stopPropagation();
            if (exact) pick(exact.name);
            else if (matches.length === 1) pick(matches[0].name);
            else if (canCreate && newAxis) pick(newAxis + ": " + query.trim());
          } else if (e.key === "Escape") {
            e.stopPropagation();
            onQuery("");
          }
        },
      }),
      !q && suggestions.length
        ? h(
            "div",
            { className: "stash-tag-curator-ta-section" },
            h("div", { className: "stash-tag-curator-ta-heading" }, "Looks similar to"),
            suggestions.map((s) =>
              h(
                "button",
                {
                  key: "s-" + s.tag,
                  type: "button",
                  className: "stash-tag-curator-ta-item",
                  onClick: () => {
                    (s.outputs || []).forEach((o) => onSelect(o));
                    onQuery("");
                  },
                },
                h("span", { className: "stash-tag-curator-ta-tag" }, s.tag),
                " → ",
                h("span", { className: "stash-tag-curator-ta-out" }, (s.outputs || []).join(", "))
              )
            )
          )
        : null,
      q
        ? h(
            "div",
            { className: "stash-tag-curator-ta-section" },
            exact
              ? h(
                  "button",
                  { type: "button", className: "stash-tag-curator-ta-item", onClick: () => pick(exact.name) },
                  h("span", { className: "stash-tag-curator-ta-tag" }, exact.name),
                  h("span", { className: "stash-tag-curator-muted" }, "  (exact)")
                )
              : null,
            matches.map((c) =>
              h(
                "button",
                { key: c.name, type: "button", className: "stash-tag-curator-ta-item", onClick: () => pick(c.name) },
                h("span", { className: "stash-tag-curator-ta-tag" }, c.name)
              )
            ),
            canCreate
              ? h(
                  "div",
                  { className: "stash-tag-curator-newtag" },
                  h("div", { className: "stash-tag-curator-ta-heading" }, "New tag in category"),
                  h(
                    "div",
                    { className: "stash-tag-curator-axis-row" },
                    AXIS_ORDER.map((axis) =>
                      h(
                        "button",
                        {
                          key: axis,
                          type: "button",
                          className: cx(
                            "stash-tag-curator-axis-chip",
                            newAxis === axis && "stash-tag-curator-axis-chip-on"
                          ),
                          onClick: () => setNewAxis(axis),
                          "aria-pressed": newAxis === axis,
                        },
                        AXIS_META[axis]
                      )
                    )
                  ),
                  h(
                    "button",
                    {
                      type: "button",
                      className: "stash-tag-curator-ta-item stash-tag-curator-newtag-btn",
                      onClick: () => pick(newAxis + ": " + query.trim()),
                    },
                    "＋ ", newAxis, ": ", query.trim()
                  )
                )
              : null
          )
        : null
    );
  }

  // ------------------------------------------------------------------
  // DictionaryPanel: the translation table. Every provider tag, its current
  // status, and three plain decisions: Translate / Keep as-is / Hide.
  // Edits are staged locally ("pending save") and applied in ONE batch via
  // Save Dictionary Edit; afterwards the panel offers to update the
  // affected scenes immediately.
  // ------------------------------------------------------------------

  function DictionaryPanel(props) {
    const snapshot = props.snapshot; // useAssetSnapshot("dictionary")
    const pending = props.pendingEdits; // {key: {status, outputs, notes, remove}}
    const onStage = props.onStage; // (key, edit|null)
    const onStageMany = props.onStageMany; // (keys[], edit|null)
    const onSave = props.onSave; // opens the save confirm modal
    const lastSave = props.lastSave;
    const onApplyEdits = props.onApplyEdits;
    const onDismissSave = props.onDismissSave;
    const onRefresh = props.onRefresh;
    const saveDisabled = !!props.saveDisabled;

    const data = snapshot.data;
    const [tab, setTab] = useState("needs_decision");
    const [search, setSearch] = useState("");
    const [cursor, setCursor] = useState(0);
    const [selected, setSelected] = useState({});
    const [editingKey, setEditingKey] = useState(null);
    const [editorQuery, setEditorQuery] = useState("");
    const [editorOutputs, setEditorOutputs] = useState([]);
    const [batchTranslate, setBatchTranslate] = useState(false);
    const listRef = useRef(null);

    const canonical = (data && data.canonical_tags) || [];
    const entries = (data && data.entries) || [];
    const stats = (data && data.stats) || {};

    const selectedKeys = Object.keys(selected).filter((k) => selected[k]);

    const filtered = useMemo(() => {
      const q = search.trim().toLowerCase();
      let list = entries.slice();
      if (tab === "pending") {
        list = list.filter((e) => pending[e.tag]);
      } else {
        list = list.filter((e) => {
          const st = pending[e.tag] ? pending[e.tag].status : e.status;
          return st === tab;
        });
      }
      if (q) {
        list = list.filter((e) => {
          if (e.tag.toLowerCase().indexOf(q) >= 0) return true;
          if ((e.outputs || []).some((o) => o.toLowerCase().indexOf(q) >= 0)) return true;
          return false;
        });
      }
      return list;
    }, [entries, tab, search, pending]);

    useEffect(() => {
      setCursor(0);
    }, [tab, search]);

    // Keep the keyboard cursor visible while triaging with j/k.
    useEffect(() => {
      const list = listRef.current;
      if (!list) return;
      const row = list.children[cursor];
      if (row && row.scrollIntoView) {
        row.scrollIntoView({ block: "nearest" });
      }
    }, [cursor]);

    function stageDecision(key, newStatus) {
      const entry = entries.find((e) => e.tag === key);
      if (newStatus === "needs_decision" || (entry && entry.status === newStatus && !pending[key])) {
        return; // no-op decision
      }
      onStage(key, { status: newStatus, outputs: [], notes: null });
      setEditingKey(null);
    }

    function openEditor(key) {
      const entry = entries.find((e) => e.tag === key);
      const staged = pending[key];
      setEditorOutputs(
        (staged && staged.outputs && staged.outputs.length
          ? staged.outputs
          : (entry && entry.outputs) || []
        ).slice()
      );
      setEditorQuery("");
      setEditingKey(key);
    }

    function commitEditor(key) {
      if (!editorOutputs.length) return;
      onStage(key, {
        status: "translated",
        outputs: editorOutputs.slice(),
        notes: null,
      });
      setEditingKey(null);
      setEditorOutputs([]);
    }

    function stageBatch(newStatus) {
      if (!selectedKeys.length) return;
      if (newStatus === "translated") {
        setBatchTranslate(true);
        setEditorOutputs([]);
        setEditorQuery("");
        return;
      }
      onStageMany(selectedKeys, { status: newStatus, outputs: [], notes: null });
      setSelected({});
    }

    function commitBatchTranslate() {
      if (!editorOutputs.length || !selectedKeys.length) return;
      onStageMany(selectedKeys, {
        status: "translated",
        outputs: editorOutputs.slice(),
        notes: null,
      });
      setSelected({});
      setBatchTranslate(false);
      setEditorOutputs([]);
      setEditorQuery("");
    }

    // Keyboard triage: j/k move, t translate, s keep, h hide, d postpone,
    // x select, Enter opens the editor (or commits its top suggestion),
    // Escape closes the editor. Skipped while typing in an input.
    function onKeyDown(e) {
      const t = e.target;
      const tag = (t && t.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key;
      if (key === "j" || key === "ArrowDown") {
        e.preventDefault();
        const maxIdx = Math.min(filtered.length - 1, 299);
        setCursor((c) => Math.min(c + 1, Math.max(maxIdx, 0)));
      } else if (key === "k" || key === "ArrowUp") {
        e.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      } else if (key === "x") {
        if (!filtered[cursor]) return;
        const k = filtered[cursor].tag;
        setSelected((prev) => {
          const next = Object.assign({}, prev);
          next[k] = !next[k];
          return next;
        });
      } else if (key === "t") {
        if (!filtered[cursor]) return;
        openEditor(filtered[cursor].tag);
      } else if (key === "s") {
        if (!filtered[cursor]) return;
        stageDecision(filtered[cursor].tag, "kept");
      } else if (key === "h") {
        if (!filtered[cursor]) return;
        stageDecision(filtered[cursor].tag, "hidden");
      } else if (key === "d") {
        if (!filtered[cursor]) return;
        stageDecision(filtered[cursor].tag, "deferred");
      } else if (key === "Enter") {
        if (editingKey) {
          commitEditor(editingKey);
        } else if (filtered[cursor]) {
          openEditor(filtered[cursor].tag);
        }
      } else if (key === "Escape") {
        setEditingKey(null);
        setBatchTranslate(false);
      }
    }

    const pendingCount = Object.keys(pending).length;

    return h(
      "div",
      { className: "stash-tag-curator-dictionary", tabIndex: 0, onKeyDown: onKeyDown },
      h(
        "div",
        { className: "stash-tag-curator-dict-toolbar" },
        h(
          "div",
          { className: "stash-tag-curator-dict-tabs" },
          DICTIONARY_TABS.map((t) => {
            const count =
              t.key === "pending"
                ? pendingCount
                : t.key === "needs_decision"
                  ? (stats.needs_decision || 0) - 0
                  : (stats[t.key] || 0);
            return h(
              "button",
              {
                key: t.key,
                type: "button",
                className: cx(
                  "stash-tag-curator-dict-tab",
                  tab === t.key && "stash-tag-curator-dict-tab-on"
                ),
                onClick: () => setTab(t.key),
              },
              t.label,
              " ",
              h("span", { className: "stash-tag-curator-dict-count" }, count)
            );
          })
        ),
        h(
          "div",
          { className: "stash-tag-curator-dict-search" },
          h(FormControl, {
            type: "search",
            value: search,
            placeholder: "Search tags or translations…",
            "aria-label": "Search the dictionary",
            onChange: (e) => setSearch(e.target.value),
          })
        )
      ),
      lastSave && !lastSave.applied && lastSave.affected_scene_count > 0
        ? h(
            "div",
            { className: "stash-tag-curator-postsave" },
            h(
              "span",
              null,
              "Your last edit affects ",
              h("strong", null, lastSave.affected_scene_count + " scenes"),
              "."
            ),
            h(
              "span",
              { className: "stash-tag-curator-postsave-actions" },
              h(BSButton, { size: "sm", variant: "primary", disabled: saveDisabled, onClick: onApplyEdits }, "Update those scenes now"),
              h(BSButton, { size: "sm", variant: "link", onClick: onDismissSave }, "Dismiss")
            )
          )
        : null,
      batchTranslate
        ? h(
            "div",
            { className: "stash-tag-curator-batchbar stash-tag-curator-batchbar-editor" },
            h("strong", null, "Translate " + selectedKeys.length + " tags to:"),
            h(CanonicalTypeahead, {
              canonical: canonical,
              query: editorQuery,
              onQuery: setEditorQuery,
              selected: editorOutputs,
              onSelect: (name) => setEditorOutputs((prev) => (prev.indexOf(name) >= 0 ? prev : prev.concat([name]))),
              onRemove: (name) => setEditorOutputs((prev) => prev.filter((o) => o !== name)),
              suggestions: [],
            }),
            h(
              "div",
              { className: "stash-tag-curator-batchbar-actions" },
              h(BSButton, { size: "sm", variant: "primary", disabled: !editorOutputs.length, onClick: commitBatchTranslate }, "Apply to all selected"),
              h(BSButton, { size: "sm", variant: "link", onClick: () => setBatchTranslate(false) }, "Cancel")
            )
          )
        : null,
      !batchTranslate && selectedKeys.length
        ? h(
            "div",
            { className: "stash-tag-curator-batchbar" },
            h("strong", null, selectedKeys.length + " selected"),
            h(BSButton, { size: "sm", variant: "outline-primary", onClick: () => stageBatch("translated") }, "Translate to…"),
            h(BSButton, { size: "sm", variant: "outline-secondary", onClick: () => stageBatch("kept") }, "Keep as-is"),
            h(BSButton, { size: "sm", variant: "outline-dark", onClick: () => stageBatch("hidden") }, "Hide"),
            h(BSButton, { size: "sm", variant: "link", onClick: () => setSelected({}) }, "Clear")
          )
        : null,
      snapshot.loading && !data
        ? h("div", { className: "stash-tag-curator-muted" }, "Loading dictionary…")
        : null,
      snapshot.error
        ? h(
            "div",
            { className: "alert alert-warning" },
            "Dictionary data unavailable (" + snapshot.error + "). ",
            h(BSButton, { size: "sm", variant: "link", onClick: onRefresh }, "Regenerate")
          )
        : null,
      data && !filtered.length
        ? h(
            "div",
            { className: "stash-tag-curator-empty" },
            tab === "needs_decision"
              ? "No tags waiting for a decision — your dictionary covers everything the providers have sent so far."
              : "Nothing here."
          )
        : null,
      h(
        "div",
        { className: "stash-tag-curator-dict-list", role: "table", "aria-label": "Tag dictionary", ref: listRef },
        filtered.slice(0, 300).map((entry, idx) => {
          const staged = pending[entry.tag] || null;
          const status = staged ? staged.status : entry.status;
          const outputs = staged && staged.outputs && staged.outputs.length ? staged.outputs : entry.outputs || [];
          const isSel = !!selected[entry.tag];
          return h(
            "div",
            {
              key: entry.tag,
              role: "row",
              className: cx(
                "stash-tag-curator-dict-row",
                idx === cursor && "stash-tag-curator-dict-row-cursor",
                isSel && "stash-tag-curator-dict-row-sel",
                staged && "stash-tag-curator-dict-row-pending"
              ),
              onClick: () => setCursor(idx),
            },
            h(
              "label",
              { className: "stash-tag-curator-dict-check" },
              h("input", {
                type: "checkbox",
                checked: isSel,
                "aria-label": "Select " + entry.tag,
                onChange: () =>
                  setSelected((prev) => {
                    const next = Object.assign({}, prev);
                    next[entry.tag] = !next[entry.tag];
                    return next;
                  }),
              })
            ),
            h(
              "div",
              { className: "stash-tag-curator-dict-main" },
              h("span", { className: "stash-tag-curator-dict-tag", title: entry.tag }, entry.tag),
              entry.scenes > 0
                ? h("span", { className: "stash-tag-curator-dict-scenes" }, entry.scenes + " scenes")
                : h("span", { className: "stash-tag-curator-dict-scenes stash-tag-curator-muted" }, "not in library"),
              staged
                ? h(BSBadge, { bg: "primary" }, "pending: " + STATUS_META[staged.status].label)
                : null
            ),
            h(
              "div",
              { className: "stash-tag-curator-dict-status" },
              h(BSBadge, { bg: (STATUS_META[status] || STATUS_META.needs_decision).variant },
                (STATUS_META[status] || STATUS_META.needs_decision).label),
              outputs.length
                ? h("span", { className: "stash-tag-curator-dict-out", title: outputs.join(", ") },
                    "→ " + outputs.join(", "))
                : null,
              entry.notes && !staged
                ? h("span", { className: "stash-tag-curator-dict-notes", title: entry.notes }, entry.notes)
                : null
            ),
            h(
              "div",
              { className: "stash-tag-curator-dict-actions" },
              editingKey === entry.tag
                ? h(
                    "div",
                    { className: "stash-tag-curator-editor" },
                    h(CanonicalTypeahead, {
                      canonical: canonical,
                      query: editorQuery,
                      onQuery: setEditorQuery,
                      selected: editorOutputs,
                      onSelect: (name) => setEditorOutputs((prev) => (prev.indexOf(name) >= 0 ? prev : prev.concat([name]))),
                      onRemove: (name) => setEditorOutputs((prev) => prev.filter((o) => o !== name)),
                      suggestions: entry.suggestions || [],
                    }),
                    h(
                      "div",
                      { className: "stash-tag-curator-editor-actions" },
                      h(BSButton, { size: "sm", variant: "primary", disabled: !editorOutputs.length, onClick: () => commitEditor(entry.tag) }, "Translate"),
                      h(BSButton, { size: "sm", variant: "outline-secondary", onClick: () => { setEditingKey(null); } }, "Cancel"),
                      entry.status !== "needs_decision"
                        ? h(
                            BSButton,
                            {
                              size: "sm",
                              variant: "link",
                              onClick: () => {
                                onStage(entry.tag, { remove: true });
                                setEditingKey(null);
                              },
                            },
                            "Remove translation"
                          )
                        : null
                    )
                  )
                : h(
                    Fragment,
                    null,
                    status === "needs_decision" || staged
                      ? h(Fragment, null,
                          h(BSButton, { size: "sm", variant: "outline-primary", onClick: () => openEditor(entry.tag) }, "Translate"),
                          h(BSButton, { size: "sm", variant: "outline-secondary", onClick: () => stageDecision(entry.tag, "kept") }, "Keep"),
                          h(BSButton, { size: "sm", variant: "outline-dark", onClick: () => stageDecision(entry.tag, "hidden") }, "Hide")
                        )
                      : h(Fragment, null,
                          h(BSButton, { size: "sm", variant: "outline-secondary", onClick: () => openEditor(entry.tag) }, "Change"),
                          staged
                            ? h(BSButton, { size: "sm", variant: "link", onClick: () => onStage(entry.tag, null) }, "Undo edit")
                            : null
                        )
                  )
            )
          );
        })
      ),
      filtered.length > 300
        ? h("div", { className: "stash-tag-curator-muted stash-tag-curator-dict-more" },
            "Showing the first 300 of " + filtered.length + " matching tags — narrow with the search box.")
        : null,
      h(
        "div",
        { className: "stash-tag-curator-savebar" },
        h(
          "div",
          { className: "stash-tag-curator-savebar-info" },
          pendingCount
            ? pendingCount + " pending change" + (pendingCount === 1 ? "" : "s") + (saveDisabled ? " (save unlocks when the run finishes)" : " (nothing saved yet)")
            : "Decisions save when you're ready — batch as many as you like.",
          h("span", { className: "stash-tag-curator-muted stash-tag-curator-kbdhint" },
            " shortcuts: j/k move · t translate · s keep · h hide · x select · enter edit")
        ),
        h(BSButton, {
          variant: "success",
          disabled: !pendingCount || saveDisabled,
          title: saveDisabled ? "Dictionary saves unlock when the current run finishes" : null,
          onClick: onSave,
        }, pendingCount ? "Save " + pendingCount + " change" + (pendingCount === 1 ? "" : "s") : "Save changes")
      )
    );
  }

  // ------------------------------------------------------------------
  // App root: two tabs, flow progress, result card, confirm gating,
  // dictionary save flow.
  // ------------------------------------------------------------------

  function App() {
    const flowState = useFlow();
    const [tab, setTab] = useState("home");
    const [refreshTick, setRefreshTick] = useState(0);
    const [opToConfirm, setOpToConfirm] = useState(null);
    const [updateOptions, setUpdateOptions] = useState({
      scanGenerate: true,
      globalCleanup: false,
    });
    const [pendingEdits, setPendingEdits] = useState(() => lsGet(LS_PENDING_EDITS, {}));
    const [lastSave, setLastSave] = useState(() => lsGet(LS_LAST_SAVE, null));
    const [saveError, setSaveError] = useState(null);
    const [genOptions, setGenOptions] = useState({
      previews: true,
      imagePreviews: true,
      phashes: true,
    });
    const [resultDismissed, setResultDismissed] = useState(false);

    const dashboard = useAssetSnapshot("dashboard", { refreshKey: refreshTick });
    const dictionary = useAssetSnapshot("dictionary", { refreshKey: refreshTick });
    const runHistory = useAssetSnapshot("run_history", { refreshKey: refreshTick });
    const runDetail = useAssetSnapshot("run_detail", { refreshKey: refreshTick });

    const busy = flowState.running;

    const refreshAll = useCallback(() => {
      setRefreshTick((t) => t + 1);
    }, []);

    // Read the plugin's Generate options once (best-effort; defaults cover
    // a failed query).
    useEffect(() => {
      gql(PLUGIN_SETTINGS_QUERY, {})
        .then((data) => {
          const cfg = data && data.configuration;
          const plugins = cfg && cfg.plugins;
          const mine =
            plugins && (plugins[PLUGIN_ID] || plugins["stash-tag-curator"]);
          if (mine && typeof mine === "object") {
            setGenOptions({
              previews: mine.generate_previews !== false,
              imagePreviews: mine.generate_image_previews !== false,
              phashes: mine.generate_phashes !== false,
            });
          }
        })
        .catch(() => {});
    }, []);

    // When a flow completes, refresh every snapshot so the result card
    // and panels reflect the finished run.
    useEffect(() => {
      if (flowState.done) {
        setResultDismissed(false);
        const t = setTimeout(() => refreshAll(), 600);
        return () => clearTimeout(t);
      }
    }, [flowState.done, refreshAll]);

    // ---- flows -------------------------------------------------------

    function startCurateFlow(preview, opts) {
      const steps = [];
      if (!preview && opts.scanGenerate) {
        steps.push({
          kind: "scan",
          label: "Scan",
          input: {
            scanGenerateCovers: true,
            scanGenerateThumbnails: true,
            rescan: false,
          },
        });
        steps.push({
          kind: "generate",
          label: "Generate",
          input: {
            previews: genOptions.previews,
            imagePreviews: genOptions.imagePreviews,
            phashes: genOptions.phashes,
            sprites: false,
            covers: false,
            transcodes: false,
            markerImagePreviews: false,
            markerScreenshots: false,
            clipPreviews: false,
            overwrite: false,
          },
        });
      }
      steps.push({
        kind: "curator",
        label: preview ? "Preview" : "Update Library",
        taskName: preview ? PREVIEW_TASK_NAME : CURATE_TASK_NAME,
        argsMap: preview
          ? { preview: "true" }
          : {
              confirmed: "true",
              cleanup_global: opts.globalCleanup ? "true" : "false",
            },
      });
      flowState.start(steps);
    }

    function runUpdate() {
      setOpToConfirm({ kind: "update" });
    }

    function runPreview() {
      startCurateFlow(true, { scanGenerate: false });
    }

    function runSimpleOp(kind) {
      const taskByKind = {
        preflight: { taskName: "Preflight", label: "Preflight" },
        validate: { taskName: "Validate Dictionary", label: "Validate dictionary" },
        refresh: { taskName: "Refresh Data", label: "Refresh data" },
      };
      const t = taskByKind[kind];
      if (!t || busy) return;
      flowState.start([
        { kind: "curator", label: t.label, taskName: t.taskName, argsMap: {} },
      ]);
    }

    // ---- dictionary save flow ----------------------------------------
    function stageEdit(key, edit) {
      setPendingEdits((prev) => {
        const next = Object.assign({}, prev);
        if (edit === null) delete next[key];
        else next[key] = edit;
        lsSet(LS_PENDING_EDITS, next);
        return next;
      });
    }

    function stageEditMany(keys, edit) {
      setPendingEdits((prev) => {
        const next = Object.assign({}, prev);
        keys.forEach((key) => {
          if (edit === null) delete next[key];
          else next[key] = edit;
        });
        lsSet(LS_PENDING_EDITS, next);
        return next;
      });
    }

    function buildSavePayload() {
      const keys = Object.keys(pendingEdits);
      const changes = [];
      const additions = [];
      const knownCanonical = {};
      ((dictionary.data && dictionary.data.canonical_tags) || []).forEach((c) => {
        knownCanonical[c.name] = true;
      });
      const entriesByKey = {};
      ((dictionary.data && dictionary.data.entries) || []).forEach((e) => {
        entriesByKey[e.tag] = e;
      });
      keys.forEach((key) => {
        const edit = pendingEdits[key];
        if (edit.remove) {
          changes.push({ normalized_key: key, remove: true });
          return;
        }
        const disposition = STATUS_TO_DISPOSITION[edit.status];
        if (!disposition) return;
        const change = { normalized_key: key, disposition: disposition };
        if (disposition === "detail" && !(edit.outputs && edit.outputs.length)) {
          // Pass-through: the tag maps to itself.  Rules validation requires
          // an explicit output; prefer the observed provider casing so the
          // Stash tag keeps its familiar spelling.
          const entry = entriesByKey[key];
          change.outputs = [(entry && entry.tag) || key];
        } else if (edit.outputs && edit.outputs.length && disposition !== "ignore") {
          change.outputs = edit.outputs.slice();
          edit.outputs.forEach((out) => {
            if (!knownCanonical[out]) {
              const parts = splitAxis(out);
              if (parts.axis) {
                additions.push({ axis: parts.axis, name: out });
                knownCanonical[out] = true;
              }
            }
          });
        }
        if (edit.notes) change.notes = edit.notes;
        changes.push(change);
      });
      return { changes: changes, additions: additions };
    }

    function openSaveConfirm() {
      const payload = buildSavePayload();
      if (!payload.changes.length) return;
      setOpToConfirm({ kind: "save", savePayload: payload });
    }

    function onSaveConfirmed() {
      const payload = opToConfirm && opToConfirm.savePayload;
      if (!payload) {
        setOpToConfirm(null);
        return;
      }
      const checksum =
        (dictionary.data && dictionary.data.rules && dictionary.data.rules.checksum) || "";
      const requestId = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
      setOpToConfirm(null);
      setSaveError(null);
      flowState.start(
        [
          {
            kind: "curator",
            label: "Save dictionary changes",
            taskName: "Save Dictionary Edit",
            argsMap: {
              expected_rules_sha: checksum,
              changes: payload.changes,
              canonical_additions: payload.additions,
              save_request_id: requestId,
            },
          },
        ],
        function () {
          verifySaveResult(requestId, payload.changes.length);
        }
      );
    }

    async function verifySaveResult(requestId, editCount) {
      // Stash marks exit-0 plugin jobs FINISHED even when the plugin itself
      // reported a handled failure, so the authoritative outcome comes from
      // the save_result side channel, keyed by our request id.
      let result = null;
      try {
        result = await fetchSnapshot("save_result");
      } catch (err) {
        result = null;
      }
      if (result && result.save_request_id && result.save_request_id === requestId) {
        if (result.saved) {
          const next = {
            at: new Date().toISOString(),
            applied: false,
            edit_count: editCount,
            affected_scene_count: result.affected_scene_count || 0,
            tags: result.affected_raw_tags || [],
          };
          setLastSave(next);
          lsSet(LS_LAST_SAVE, next);
          setPendingEdits({});
          lsSet(LS_PENDING_EDITS, {});
          refreshAll();
        } else if (result.error === "rules_changed") {
          setSaveError(
            "The dictionary changed elsewhere since you opened this page. " +
              "Your decisions are kept as a draft - review and save again."
          );
          refreshAll();
        } else if (result.error === "run_lock_active") {
          setSaveError(
            "A curator run is active; the dictionary can't be edited right now. " +
              "Try again when it finishes."
          );
        } else if (result.error === "validation_failed") {
          setSaveError(
            "The dictionary rejected the change: " +
              ((result.errors || []).join("; ") || "validation failed")
          );
        } else {
          setSaveError("Save failed" + (result.message ? ": " + result.message : "."));
        }
        return;
      }
      // Side-channel unreadable: do NOT infer success from a checksum move
      // (an unrelated writer could have caused it) and do NOT clear the
      // draft.  Tell the operator to retry; staged decisions are kept.
      setSaveError(
        "Could not confirm whether the save went through. Your decisions " +
          "are kept - check the Dictionary tab and save again."
      );
      refreshAll();
    }

    // ---- close the loop: apply edits to affected scenes -------------
    function applyEdits() {
      if (!lastSave || lastSave.applied) return;
      const tags = lastSave.tags || [];
      setOpToConfirm({
        kind: "apply-edits",
        affectedTags: tags,
      });
    }

    function dismissSave() {
      if (lastSave) {
        const next = Object.assign({}, lastSave, { applied: true });
        setLastSave(next);
        lsSet(LS_LAST_SAVE, next);
      }
    }

    // ---- result card target -----------------------------------------
    const doneRun = useMemo(() => {
      if (!flowState.done || resultDismissed) return null;
      const runs = (runHistory.data && runHistory.data.runs) || [];
      return runs.find((r) => !r.parent_run_id) || null;
    }, [flowState.done, resultDismissed, runHistory.data]);

    const previewDone = !!(flowState.done && !flowState.done.failed &&
      flowState.done.steps.some((s) => s.taskName === PREVIEW_TASK_NAME));

    function confirmUpdate() {
      setOpToConfirm(null);
      startCurateFlow(false, updateOptions);
    }

    const modal = (() => {
      if (!opToConfirm) return null;
      if (opToConfirm.kind === "update") {
        const d = dashboard.data || {};
        const totals = d.totals || {};
        const lines = [];
        if ((totals.never_processed || 0) > 0)
          lines.push(totals.never_processed + " new scenes to identify");
        if ((totals.stale || 0) > 0)
          lines.push(totals.stale + " out-of-date scenes to re-check");
        if ((totals.failed || 0) > 0)
          lines.push(totals.failed + " previous failures to retry");
        if (!lines.length) lines.push("No scenes are waiting — the run will be quick.");
        return h(
          ConfirmModal,
          {
            title: "Update Library",
            summary:
              "Identifies new scenes via your providers, applies your dictionary, " +
              "fills in missing info, creates missing performers/studios (capped), " +
              "removes unused curator tags, and retries previous failures.",
            scopeLabel: "your whole library",
            lines: lines,
            options: [
              {
                key: "scanGenerate",
                label: "Scan & generate new files first (recommended)",
                checked: updateOptions.scanGenerate,
                onChange: (v) => setUpdateOptions((p) => Object.assign({}, p, { scanGenerate: v })),
              },
              {
                key: "globalCleanup",
                label:
                  "Also remove unused tags NOT created by the curator (any tag with zero uses anywhere)",
                checked: updateOptions.globalCleanup,
                onChange: (v) => setUpdateOptions((p) => Object.assign({}, p, { globalCleanup: v })),
              },
            ],
            confirmText: "Update Library",
            onClose: () => setOpToConfirm(null),
            onConfirm: confirmUpdate,
          }
        );
      }
      if (opToConfirm.kind === "apply-edits") {
        const tags = opToConfirm.affectedTags || [];
        return h(
          ConfirmModal,
          {
            title: "Update affected scenes",
            summary:
              "Runs Update Library scoped to the scenes your latest dictionary " +
              "edits touch.",
            scopeLabel: tags.length
              ? "scenes carrying " + (tags.length === 1 ? "1 edited tag" : tags.length + " edited tags")
              : "scenes touched by the edited tags",
            lines: [],
            options: [],
            confirmText: "Update those scenes",
            onClose: () => setOpToConfirm(null),
            onConfirm: () => {
              setOpToConfirm(null);
              flowState.start([
                {
                  kind: "curator",
                  label: "Update Library",
                  taskName: CURATE_TASK_NAME,
                  argsMap: {
                    confirmed: "true",
                    cleanup_global: "false",
                    affected_raw_tags: tags,
                  },
                },
              ]);
            },
          }
        );
      }
      if (opToConfirm.kind === "save") {
        const payload = opToConfirm.savePayload;
        return h(
          ConfirmModal,
          {
            title: "Save dictionary changes",
            summary:
              "Writes your pending decisions to the dictionary file. Nothing " +
              "is applied to scenes yet - you'll be offered a scoped update right after.",
            scopeLabel:
              payload.changes.length + " tag decision" +
              (payload.changes.length === 1 ? "" : "s"),
            lines: [],
            options: [],
            confirmText: "Save changes",
            onClose: () => setOpToConfirm(null),
            onConfirm: onSaveConfirmed,
          }
        );
      }
      return null;
    })();

    return h(
      "div",
      { className: "stash-tag-curator-app" },
      h(
        "div",
        { className: "stash-tag-curator-header" },
        h("h2", null, "Tag Curator"),
        h("p", { className: "stash-tag-curator-muted" },
          "Your providers' tags, translated into your tags.")
      ),
      h(ProgressPanel, {
        flow: flowState.flow,
        job: flowState.job,
        error: flowState.error,
        onCancel: flowState.cancel,
        onDismiss: flowState.dismiss,
      }),
      doneRun
        ? h(ResultCard, {
            run: doneRun,
            detail: runDetail.data,
            needsDecision: dictionary.data
              ? (dictionary.data.stats || {}).needs_decision
              : null,
            preview: previewDone,
            onDismiss: () => setResultDismissed(true),
            onGotoDictionary: () => setTab("dictionary"),
          })
        : null,
      saveError
        ? h(
            "div",
            { className: "alert alert-warning stash-tag-curator-saveerror", role: "alert" },
            saveError,
            " ",
            h(BSButton, { variant: "link", size: "sm", onClick: () => setSaveError(null) }, "dismiss")
          )
        : null,
      h(
        BSTabs,
        {
          activeKey: tab,
          onSelect: (k) => setTab(k),
          id: "stash-tag-curator-tabs",
          className: "stash-tag-curator-tabs",
        },
        h(BSTab, { eventKey: "home", title: "Home" },
          h(HomePanel, {
            flowState: flowState,
            dashboard: dashboard,
            dictionary: dictionary,
            runHistory: runHistory,
            lastSave: lastSave,
            onRunUpdate: runUpdate,
            onRunPreview: runPreview,
            onGoto: setTab,
            onApplyEdits: applyEdits,
            onDismissSave: dismissSave,
            onRunOp: runSimpleOp,
            onSelectRun: () => {},
            onRefresh: () => runSimpleOp("refresh"),
            refreshing: busy,
            busy: busy,
          })
        ),
        h(BSTab, { eventKey: "dictionary", title: "Dictionary" },
          h(DictionaryPanel, {
            snapshot: dictionary,
            pendingEdits: pendingEdits,
            onStage: stageEdit,
            onStageMany: stageEditMany,
            onSave: openSaveConfirm,
            saveDisabled: busy,
            lastSave: lastSave,
            onApplyEdits: applyEdits,
            onDismissSave: dismissSave,
            onRefresh: () => runSimpleOp("refresh"),
          })
        )
      ),
      modal
    );
  }

  // ------------------------------------------------------------------
  // Nav-bar patch: Stash v0.31.1 plugin routes are client-side only, so
  // users need an in-app link to enter /plugin/stash-tag-curator. The
  // PluginApi.patch surface is experimental; this callback must always
  // return a valid argument list and fall back to Stash's original props.
  // ------------------------------------------------------------------

  try {
    var RRDOM = (api.libraries && api.libraries.ReactRouterDOM) || {};
    var FAS = (api.libraries && api.libraries.FontAwesomeSolid) || {};
    var IconCmp = (api.components && api.components.Icon) || null;
    var hasNavLink =
      RRDOM.NavLink &&
      (typeof RRDOM.NavLink === "function" ||
        (typeof RRDOM.NavLink === "object" && RRDOM.NavLink.render));

    if (
      api.patch &&
      typeof api.patch.before === "function" &&
      hasNavLink
    ) {
      var NavLink = RRDOM.NavLink;
      var navIcon = FAS.faTags || null;

      api.patch.before("MainNavBar.MenuItems", function (props) {
        try {
          if (!props || typeof props !== "object") {
            return [{}];
          }
          var existing = props.children != null ? props.children : null;
          var icon =
            navIcon && typeof IconCmp === "function"
              ? h(IconCmp, {
                  icon: navIcon,
                  className: "nav-menu-icon d-block d-xl-inline mb-2 mb-xl-0",
                })
              : null;
          var tile = h(
            "div",
            { key: "stash-tag-curator-nav", className: "col-4 col-sm-3 col-md-2 col-lg-auto" },
            h(
              NavLink,
              {
                exact: true,
                to: ROUTE_PATH,
                activeClassName: "active",
                className:
                  "minimal p-4 p-xl-2 d-flex d-xl-inline-block flex-column justify-content-between align-items-center btn btn-primary",
              },
              icon,
              h("span", null, "Tag Curator")
            )
          );
          return [
            { children: h(React.Fragment, null, existing, tile) },
          ];
        } catch (navPatchError) {
          console.error(
            "[stash-tag-curator] nav patch render failed; leaving Stash nav unchanged",
            navPatchError
          );
          return [props || {}];
        }
      });
    } else {
      console.warn(
        "[stash-tag-curator] nav patch skipped: PluginApi.patch.before or NavLink unavailable"
      );
    }
  } catch (navError) {
    console.error("[stash-tag-curator] nav patch registration failed", navError);
  }

  try {
    api.register.route(ROUTE_PATH, App);
  } catch (error) {
    console.error("[stash-tag-curator] route registration failed", error);
  }
})();

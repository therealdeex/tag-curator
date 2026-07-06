// Stash Tag Curator - UI route (T22 part 1).
//
// Registers `/plugin/stash-tag-curator` through Stash's experimental PluginApi
// and renders the dashboard + operations panels. Plain JS (no JSX, no build
// step). All React access goes through `PluginApi.React`; Bootstrap components
// come from `PluginApi.libraries.Bootstrap`. No second React copy is bundled.
//
// Routing follows decisions D14 (read asset snapshots during a mutation run)
// and D5/D21 (cancellation is Stash `stopJob`, a SIGKILL; lock goes stale for
// manual recovery).
//
// SECURITY: every user-controlled string (tag names, run IDs, task labels) is
// rendered as a React text child. React escapes text children by default, so
// the values cannot break out into HTML. NEVER inject curator data via the
// React escape-hatch API; only text children are permitted.

"use strict";

(function () {
  const api = window.PluginApi;
  if (!api || !api.React || !api.register || !api.register.route) {
    console.warn("[stash-tag-curator] compatible PluginApi not available");
    return;
  }

  // ------------------------------------------------------------------
  // Constants
  // ------------------------------------------------------------------

  const PLUGIN_ID = "stash-tag-curator";
  const ROUTE_PATH = "/plugin/stash-tag-curator";
  const ASSET_BASE = "/plugin/stash-tag-curator/assets/";
  const GRAPHQL_ENDPOINT = "/graphql";
  const POLL_INTERVAL_MS = 1000;
  const DASHBOARD_REFRESH_MS = 5000;
  const LOCALSTORAGE_KEY = "stashTagCurator.activeJob";

  // Stash job status values that mean "no more polling needed". Kept broad so
  // future Stash status names still terminate the loop.
  const TERMINAL_STATUSES = new Set([
    "COMPLETE",
    "COMPLETED",
    "FAILED",
    "FAILURE",
    "CANCELLED",
    "CANCELED",
    "STOPPING",
    "STOPPED",
    "REMOVED",
  ]);

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
        addTime
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

  // ------------------------------------------------------------------
  // React + Bootstrap shortcuts
  // ------------------------------------------------------------------

  const React = api.React;
  const h = React.createElement;
  const useState = React.useState;
  const useEffect = React.useEffect;
  const useRef = React.useRef;
  const useCallback = React.useCallback;
  const useMemo = React.useMemo;

  const Bootstrap = (api.libraries && api.libraries.Bootstrap) || {};
  const BSButton = Bootstrap.Button || "button";
  const BSAlert = Bootstrap.Alert || "div";
  const BSBadge = Bootstrap.Badge || "span";
  const BSCard = Bootstrap.Card || "div";
  const BSFormGroup = (Bootstrap.Form && Bootstrap.Form.Group) || "div";
  const BSFormControl = (Bootstrap.Form && Bootstrap.Form.Control) || "input";
  const BSSpinner = Bootstrap.Spinner || "span";
  const BSTabs = Bootstrap.Tabs || "div";
  const BSTab = Bootstrap.Tab || "div";
  const BSModal = Bootstrap.Modal || null;
  const BSModalHeader = (Bootstrap.Modal && Bootstrap.Modal.Header) || null;
  const BSModalTitle = (Bootstrap.Modal && Bootstrap.Modal.Title) || null;
  const BSModalBody = (Bootstrap.Modal && Bootstrap.Modal.Body) || null;
  const BSModalFooter = (Bootstrap.Modal && Bootstrap.Modal.Footer) || null;

  // ------------------------------------------------------------------
  // GraphQL + asset helpers
  // ------------------------------------------------------------------

  async function gql(query, variables) {
    let response;
    try {
      response = await fetch(GRAPHQL_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ query: query, variables: variables || {} }),
      });
    } catch (networkErr) {
      throw new Error(
        "network error contacting Stash GraphQL endpoint: " +
          (networkErr && networkErr.message ? networkErr.message : String(networkErr))
      );
    }
    if (!response.ok) {
      throw new Error("GraphQL HTTP " + response.status);
    }
    let body;
    try {
      body = await response.json();
    } catch (parseErr) {
      throw new Error("GraphQL response was not valid JSON");
    }
    if (body && body.errors && body.errors.length) {
      const messages = body.errors
        .map((e) => (e && e.message ? e.message : String(e)))
        .join("; ");
      throw new Error("GraphQL errors: " + messages);
    }
    if (!body || typeof body.data !== "object") {
      throw new Error("GraphQL response missing data");
    }
    return body.data;
  }

  async function fetchDashboardSnapshot(cacheBust) {
    const url =
      ASSET_BASE + "dashboard.json" + (cacheBust ? "?_=" + Date.now() : "");
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("dashboard asset HTTP " + response.status);
    }
    return response.json();
  }

  // ------------------------------------------------------------------
  // localStorage persistence (D21: survive page reloads)
  // ------------------------------------------------------------------

  function loadActiveJob() {
    try {
      const raw = window.localStorage.getItem(LOCALSTORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || !parsed.job_id) return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  function persistActiveJob(job) {
    try {
      if (!job) {
        window.localStorage.removeItem(LOCALSTORAGE_KEY);
        return;
      }
      // Persist ONLY job metadata. Never secrets.
      window.localStorage.setItem(
        LOCALSTORAGE_KEY,
        JSON.stringify({
          job_id: job.job_id,
          task_name: job.task_name,
          label: job.label || null,
          status: job.status,
          progress: job.progress,
          started_at: job.started_at,
          args_label: job.args_label || null,
        })
      );
    } catch (err) {
      // localStorage unavailable (private mode, disabled) - non-fatal.
    }
  }

  // ------------------------------------------------------------------
  // Misc helpers
  // ------------------------------------------------------------------

  function isTerminalStatus(status) {
    if (!status) return false;
    return TERMINAL_STATUSES.has(String(status).toUpperCase());
  }

  function formatPercent(progress) {
    const n = typeof progress === "number" ? progress : parseFloat(progress);
    if (!isFinite(n)) return "0%";
    // Stash reports progress in the range 0..1.
    const pct = Math.max(0, Math.min(100, Math.round(n * 100)));
    return pct + "%";
  }

  // Render values that may be missing without crashing. Numbers/strings/bools
  // are stringified; objects become a muted placeholder.
  function renderValue(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return h("span", { className: "stash-tag-curator-muted" }, fallback || "\u2014");
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return h("span", null, String(value));
    }
    if (typeof value === "string") {
      return h("span", null, value);
    }
    return h("span", { className: "stash-tag-curator-muted" }, fallback || "\u2014");
  }

  function formatTimestamp(iso) {
    if (!iso) return null;
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleString();
    } catch (err) {
      return String(iso);
    }
  }

  // ------------------------------------------------------------------
  // Operations registry
  // ------------------------------------------------------------------
  //
  // Every operation maps a UI button to a curator task name (camel-case
  // manifest token; the dispatcher normalises it). `destructive: true` forces
  // a confirmation modal before dispatch. `estimateFromTotals(t)` returns a
  // number used in the confirm modal summary when available.

  const OPERATIONS = [
    {
      key: "dryRebuild",
      label: "Dry-Run Full Library Rebuild",
      taskName: "DryRebuild",
      destructive: false,
      scope:
        "Simulate a full library rebuild across every scene. No mutations are performed; a dry-run proposal is written for review.",
      estimateFromTotals: (t) => (t ? t.total_scenes : null),
      estimateLabel: "scenes would be evaluated",
      argsMap: { dryRun: "true" },
    },
    {
      key: "rebuild",
      label: "Full Library Rebuild",
      taskName: "Rebuild",
      destructive: true,
      scope:
        "Re-derive every scene's tags from provider metadata. CURATOR-owned tags are fully replaced; protected tags are preserved.",
      estimateFromTotals: (t) => (t ? t.total_scenes : null),
      estimateLabel: "scenes will be reprocessed",
      argsMap: { dryRun: "false" },
    },
    {
      key: "processNew",
      label: "Process New and Unprocessed",
      taskName: "ProcessNew",
      destructive: true,
      scope:
        "Run the curation pass over scenes that have no prior curator state. Tag sets are fully replaced per the active rules.",
      estimateFromTotals: (t) => (t ? t.never_processed : null),
      estimateLabel: "scenes will be processed",
      argsMap: {},
    },
    {
      key: "reprocessStale",
      label: "Reprocess Stale",
      taskName: "ReprocessStale",
      destructive: true,
      scope:
        "Re-run the curation pass over scenes whose last processing predates the current rules or provider fingerprint.",
      estimateFromTotals: (t) => (t ? t.stale : null),
      estimateLabel: "stale scenes will be reprocessed",
      argsMap: {},
    },
    {
      key: "enrich",
      label: "Enrich from Performer Metadata",
      taskName: "Enrich",
      destructive: true,
      scope:
        "Derive additional tags from performer metadata (cast, demographics, body, theme) without a full provider re-scrape.",
      estimateFromTotals: (t) => (t ? t.processed || t.total_scenes : null),
      estimateLabel: "scenes would be enriched",
      argsMap: {},
    },
    {
      key: "cleanupSafe",
      label: "Remove Unused Tags",
      taskName: "CleanupSafe",
      destructive: true,
      scope:
        "Identify orphaned tags across the library and remove tags with zero associations that are not owned by the curator or marked protected. A two-step flow: dry-run proposal first, then execute.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      twoPhase: true,
    },
    {
      key: "rollback",
      label: "Rollback a Run",
      taskName: "Rollback",
      destructive: true,
      scope:
        "Revert the effects of a prior run by restoring each affected scene's recorded pre-run tag set. Conflict policy: skip-with-warning.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      requiresRunId: true,
    },
    {
      key: "resumeRun",
      label: "Resume Interrupted Run",
      taskName: "ResumeRun",
      destructive: true,
      scope:
        "Resume an interrupted rebuild-family run from its last checkpoint. Reconciles pending mutations and continues processing.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      requiresRunId: true,
    },
    {
      key: "abandonRun",
      label: "Abandon Interrupted Run",
      taskName: "AbandonRun",
      destructive: true,
      scope:
        "Mark an interrupted run as abandoned and release its lock if still held. Keeps history for audit.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      requiresRunId: true,
    },
    {
      key: "forceRelease",
      label: "Force Release Stale Run",
      taskName: "ForceRelease",
      destructive: true,
      scope:
        "Audited override: release a stale singleton run lock. Use only when the lock holder is dead (e.g. after SIGKILL).",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      requiresRunId: true,
    },
    {
      key: "undoCleanup",
      label: "Undo Cleanup",
      taskName: "UndoCleanup",
      destructive: true,
      scope:
        "Restore tags removed by a prior cleanup run using the recorded deletion journal.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
      requiresCleanupRunId: true,
    },
    {
      key: "validateRules",
      label: "Validate Rules",
      taskName: "ValidateRules",
      destructive: false,
      scope:
        "Read-only structural and semantic validation of the active rules file. No mutations.",
      estimateFromTotals: null,
      estimateLabel: null,
      argsMap: {},
    },
  ];

  // ------------------------------------------------------------------
  // Hooks
  // ------------------------------------------------------------------

  // `useJob` owns a single active curator job lifecycle: dispatch, poll
  // `findJob`, restore from localStorage, and expose `cancel` (Stash stopJob).
  function useJob() {
    const [job, setJob] = useState(loadActiveJob);
    const [dispatchError, setDispatchError] = useState(null);
    const [pollError, setPollError] = useState(null);
    const [result, setResult] = useState(null); // {status, finalJob}
    const timerRef = useRef(null);
    const job_id_ref = useRef(job ? job.job_id : null);
    const onCompleteRef = useRef(null);

    const clearTimer = useCallback(() => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }, []);


    const pollOnce = useCallback(
      async (targetJobId) => {
        try {
          const data = await gql(FIND_JOB_QUERY, { id: targetJobId });
          const j = (data && data.findJob) || null;
          if (!j) {
            // Job vanished from the queue (finished + GC'd by Stash). Treat as
            // terminal so the UI doesn't spin forever.
            setPollError(null);
            setJob((prev) => {
              const finalJob = Object.assign({}, prev, {
                status: "REMOVED",
                ended_at: new Date().toISOString(),
              });
              onCompleteRef.current && onCompleteRef.current(finalJob);
              onCompleteRef.current = null;
              return finalJob;
            });
            persistActiveJob(null);
            clearTimer();
            return;
          }
          const status = String(j.status || "").toUpperCase();
          const updated = {
            job_id: targetJobId,
            task_name: (job && job.task_name) || null,
            label: (job && job.label) || null,
            args_label: (job && job.args_label) || null,
            status: status,
            progress: typeof j.progress === "number" ? j.progress : 0,
            description: j.description || null,
            error: j.error || null,
            sub_tasks: j.subTasks || null,
            start_time: j.startTime || null,
            end_time: j.endTime || null,
          };
          setPollError(null);
          setJob(updated);
          if (isTerminalStatus(status)) {
            persistActiveJob(null);
            clearTimer();
            setResult({ status: status, finalJob: updated });
            if (onCompleteRef.current) {
              onCompleteRef.current(updated);
              onCompleteRef.current = null;
            }
          } else {
            persistActiveJob(updated);
            timerRef.current = setTimeout(() => {
              pollOnce(targetJobId);
            }, POLL_INTERVAL_MS);
          }
        } catch (err) {
          setPollError(
            "polling error: " + (err && err.message ? err.message : String(err))
          );
          // Backoff but keep polling - transient network failures shouldn't
          // abandon an active run.
          timerRef.current = setTimeout(() => {
            pollOnce(targetJobId);
          }, POLL_INTERVAL_MS * 2);
        }
      },
      [job, clearTimer]
    );

    const dispatch = useCallback(
      (opts) => {
        // opts: {taskName, argsMap, label, argsLabel, onComplete}
        if (job && !isTerminalStatus(job.status)) {
          setDispatchError(
            "a job is already running (" +
              (job.task_name || job.job_id) +
              "); wait for it to finish or cancel it first"
          );
          return;
        }
        setDispatchError(null);
        setPollError(null);
        setResult(null);
        gql(RUN_PLUGIN_TASK_MUTATION, {
          plugin_id: PLUGIN_ID,
          task_name: opts.taskName,
          description: opts.label || opts.taskName,
          args_map: opts.argsMap || {},
        })
          .then((data) => {
            const jobId = data && data.runPluginTask;
            if (!jobId) {
              throw new Error("runPluginTask returned no job id");
            }
            const initial = {
              job_id: jobId,
              task_name: opts.taskName,
              label: opts.label || opts.taskName,
              args_label: opts.argsLabel || null,
              status: "READY",
              progress: 0,
              started_at: new Date().toISOString(),
            };
            job_id_ref.current = jobId;
            onCompleteRef.current = opts.onComplete || null;
            setJob(initial);
            persistActiveJob(initial);
            clearTimer();
            timerRef.current = setTimeout(() => {
              pollOnce(jobId);
            }, POLL_INTERVAL_MS);
          })
          .catch((err) => {
            setDispatchError(
              "failed to start job: " +
                (err && err.message ? err.message : String(err))
            );
            setJob(null);
            persistActiveJob(null);
          });
      },
      [job, pollOnce, clearTimer]
    );

    const cancel = useCallback(() => {
      const current = job_id_ref.current;
      if (!current) return;
      gql(STOP_JOB_MUTATION, { job_id: current })
        .then(() => {
          // Per D5/D21: stopJob SIGKILLs the raw task. We keep polling until
          // Stash confirms the terminal status, then clear local state.
        })
        .catch((err) => {
          setPollError(
            "cancel failed: " + (err && err.message ? err.message : String(err))
          );
        });
    }, []);

    const dismiss = useCallback(() => {
      clearTimer();
      job_id_ref.current = null;
      onCompleteRef.current = null;
      setJob(null);
      setResult(null);
      setDispatchError(null);
      setPollError(null);
      persistActiveJob(null);
    }, [clearTimer]);

    // Rehydrate polling on mount if a job was still active when the page
    // reloaded (D21).
    useEffect(() => {
      const saved = loadActiveJob();
      if (saved && saved.job_id && !isTerminalStatus(saved.status)) {
        job_id_ref.current = saved.job_id;
        clearTimer();
        timerRef.current = setTimeout(() => {
          pollOnce(saved.job_id);
        }, POLL_INTERVAL_MS);
      }
      return () => {
        clearTimer();
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return {
      job: job,
      dispatchError: dispatchError,
      pollError: pollError,
      result: result,
      dispatch: dispatch,
      cancel: cancel,
      dismiss: dismiss,
    };
  }

  // `useDashboard` fetches the dashboard asset and re-fetches periodically
  // while a job is running (D14: the engine writes snapshots during a run).
  function useDashboard(activeJobStatus) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [lastUpdated, setLastUpdated] = useState(null);
    const timerRef = useRef(null);

    const fetchOnce = useCallback(async () => {
      try {
        const snapshot = await fetchDashboardSnapshot(true);
        setData(snapshot);
        setError(null);
        setLastUpdated(new Date().toISOString());
      } catch (err) {
        setError(
          err && err.message ? err.message : String(err)
        );
      } finally {
        setLoading(false);
      }
    }, []);

    const refresh = useCallback(() => {
      fetchOnce();
    }, [fetchOnce]);

    useEffect(() => {
      fetchOnce();
    }, [fetchOnce]);

    // Poll the asset while a job is running.
    useEffect(() => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (activeJobStatus && !isTerminalStatus(activeJobStatus)) {
        const tick = () => {
          fetchOnce().finally(() => {
            timerRef.current = setTimeout(tick, DASHBOARD_REFRESH_MS);
          });
        };
        timerRef.current = setTimeout(tick, DASHBOARD_REFRESH_MS);
      }
      return () => {
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
      };
    }, [activeJobStatus, fetchOnce]);

    return {
      data: data,
      error: error,
      loading: loading,
      lastUpdated: lastUpdated,
      refresh: refresh,
    };
  }

  // ------------------------------------------------------------------
  // Modal helper
  // ------------------------------------------------------------------
  //
  // Uses react-bootstrap Modal when available; otherwise renders a simple
  // fixed-position overlay. Either way the contents are React children so all
  // string data is escaped by React.

  function ModalShell(props) {
    const show = !!props.show;
    const onHide = props.onHide || function () {};
    const title = props.title || "";
    const children = props.children || null;
    const footer = props.footer || null;
    const dialogClassName =
      "stash-tag-curator-modal" +
      (props.size === "large" ? " stash-tag-curator-modal-lg" : "");

    if (BSModal) {
      return h(
        BSModal,
        {
          show: show,
          onHide: onHide,
          size: props.size === "large" ? "lg" : undefined,
          backdrop: "static",
          keyboard: true,
          className: dialogClassName,
        },
        BSModalHeader &&
          h(
            BSModalHeader,
            { closeButton: true },
            BSModalTitle && h(BSModalTitle, null, title)
          ),
        BSModalBody && h(BSModalBody, null, children),
        footer && BSModalFooter && h(BSModalFooter, null, footer)
      );
    }
    if (!show) return null;
    return h(
      "div",
      {
        className: dialogClassName + " stash-tag-curator-modal-fallback",
        role: "dialog",
        "aria-modal": "true",
      },
      h(
        "div",
        { className: "stash-tag-curator-modal-inner" },
        h(
          "div",
          { className: "stash-tag-curator-modal-header" },
          h("h3", null, title),
          h(
            "button",
            {
              type: "button",
              className: "stash-tag-curator-modal-close",
              onClick: onHide,
              "aria-label": "Close",
            },
            "\u00d7"
          )
        ),
        h("div", { className: "stash-tag-curator-modal-body" }, children),
        footer &&
          h("div", { className: "stash-tag-curator-modal-footer" }, footer)
      )
    );
  }

  // ------------------------------------------------------------------
  // Dashboard panel
  // ------------------------------------------------------------------

  function StatCard(props) {
    return h(
      BSCard,
      { className: "stash-tag-curator-stat-card" },
      h(
        "div",
        { className: "stash-tag-curator-stat-value" },
        props.value !== null && props.value !== undefined
          ? String(props.value)
          : "\u2014"
      ),
      h(
        "div",
        { className: "stash-tag-curator-stat-label" },
        props.label || ""
      )
    );
  }

  function DashboardPanel(props) {
    const dashboard = props.dashboard;
    const data = dashboard.data;
    const loading = dashboard.loading;

    if (loading && !data) {
      return h(
        "div",
        { className: "stash-tag-curator-loading" },
        h(BSSpinner, { animation: "border", size: "sm" }),
        " ",
        "Loading dashboard..."
      );
    }

    if (!data && dashboard.error) {
      return h(
        BSAlert,
        { variant: "warning", className: "stash-tag-curator-alert" },
        h("strong", null, "Dashboard snapshot unavailable."),
        " ",
        "Run any mutation task or the Dashboard read task to generate one. ",
        h("br", null),
        "Detail: ",
        String(dashboard.error)
      );
    }

    const totals = (data && data.totals) || {};
    const rules = (data && data.rules) || {};
    const lastRun = (data && data.last_successful_run) || null;
    const activeJob = (data && data.active_job) || null;
    const providers = (data && data.configured_providers) || [];
    const recentErrors = (data && data.recent_errors) || [];
    const generatedAt = formatTimestamp(data && data.generated_at);

    return h(
      "div",
      { className: "stash-tag-curator-dashboard" },
      h(
        "div",
        { className: "stash-tag-curator-dashboard-toolbar" },
        h(
          "span",
          { className: "stash-tag-curator-muted stash-tag-curator-updated" },
          generatedAt ? "Updated " + generatedAt : ""
        ),
        h(
          BSButton,
          {
            type: "button",
            size: "sm",
            variant: "secondary",
            onClick: dashboard.refresh,
            disabled: dashboard.loading,
          },
          dashboard.loading ? "Refreshing..." : "Refresh"
        )
      ),
      dashboard.error &&
        h(
          BSAlert,
          { variant: "secondary", className: "stash-tag-curator-alert" },
          "Last refresh warning: ",
          String(dashboard.error)
        ),
      h(
        "div",
        { className: "stash-tag-curator-stat-grid" },
        h(StatCard, {
          label: "Total scenes",
          value: totals.total_scenes,
        }),
        h(StatCard, {
          label: "Processed",
          value: totals.processed,
        }),
        h(StatCard, {
          label: "Never processed",
          value: totals.never_processed,
        }),
        h(StatCard, {
          label: "Stale",
          value: totals.stale,
        }),
        h(StatCard, {
          label: "Failed",
          value: totals.failed,
        }),
        h(StatCard, {
          label: "Scenes with unmapped tags",
          value: totals.scenes_with_unmapped_tags,
        }),
        h(StatCard, {
          label: "Unmapped raw-tag count",
          value: data && data.unmapped_raw_tag_count,
        })
      ),
      h(
        "section",
        { className: "stash-tag-curator-section" },
        h("h4", null, "Rules"),
        h(
          "dl",
          { className: "stash-tag-curator-kv" },
          h("dt", null, "Version"),
          h("dd", null, renderValue(rules.version)),
          h("dt", null, "Checksum"),
          h(
            "dd",
            { className: "stash-tag-curator-mono" },
            h(
              "span",
              { className: "stash-tag-curator-mono-trunc" },
              rules.checksum || "\u2014"
            )
          )
        )
      ),
      h(
        "section",
        { className: "stash-tag-curator-section" },
        h("h4", null, "Last successful run"),
        lastRun
          ? h(
              "dl",
              { className: "stash-tag-curator-kv" },
              h("dt", null, "Operation"),
              h("dd", null, renderValue(lastRun.operation)),
              h("dt", null, "Ended"),
              h("dd", null, renderValue(formatTimestamp(lastRun.ended_at))),
              h("dt", null, "Run ID"),
              h(
                "dd",
                { className: "stash-tag-curator-mono" },
                renderValue(lastRun.run_id)
              )
            )
          : h("p", { className: "stash-tag-curator-muted" }, "No successful runs yet.")
      ),
      h(
        "section",
        { className: "stash-tag-curator-section" },
        h("h4", null, "Active job"),
        activeJob && activeJob.held
          ? h(
              BSAlert,
              { variant: "warning", className: "stash-tag-curator-alert" },
              "A run lock is currently held",
              activeJob.run_id ? " (run " + activeJob.run_id + ")" : "",
              ". ",
              activeJob.stale
                ? "The lock appears stale; use Force Release to clear it."
                : "Reads use the live asset snapshot until the run completes."
            )
          : h("p", { className: "stash-tag-curator-muted" }, "No active job.")
      ),
      h(
        "section",
        { className: "stash-tag-curator-section" },
        h("h4", null, "Configured providers"),
        providers.length
          ? h(
              "ul",
              { className: "stash-tag-curator-provider-list" },
              providers.map((p, i) =>
                h("li", { key: "provider-" + i }, String(p))
              )
            )
          : h("p", { className: "stash-tag-curator-muted" }, "None configured.")
      ),
      recentErrors.length > 0 &&
        h(
          "section",
          { className: "stash-tag-curator-section" },
          h("h4", null, "Recent errors"),
          h(
            "ul",
            { className: "stash-tag-curator-error-list" },
            recentErrors.slice(0, 10).map((err, i) =>
              h(
                "li",
                { key: "err-" + i, className: "stash-tag-curator-error-item" },
                h(
                  "span",
                  { className: "stash-tag-curator-muted" },
                  formatTimestamp(err.timestamp || err.at || err.occurred_at) || ""
                ),
                " ",
                String(err.message || err.error || JSON.stringify(err))
              )
            )
          )
        )
    );
  }

  // ------------------------------------------------------------------
  // Operations panel
  // ------------------------------------------------------------------

  function OperationsPanel(props) {
    const dashboard = props.dashboard;
    const jobState = props.jobState;
    const opToConfirm = props.opToConfirm;
    const setOpToConfirm = props.setOpToConfirm;
    const pendingArgs = props.pendingArgs;
    const setPendingArgs = props.setPendingArgs;
    const cleanupProposal = props.cleanupProposal;
    const setCleanupProposal = props.setCleanupProposal;

    const totals = (dashboard.data && dashboard.data.totals) || {};
    const jobInProgress = !!(jobState.job && !isTerminalStatus(jobState.job.status));

    function openConfirm(op) {
      const next = Object.assign({}, pendingArgs);
      let changed = false;
      if (op.requiresRunId && !next.run_id) {
        next.run_id = "";
        changed = true;
      }
      if (op.requiresCleanupRunId && !next.cleanup_run_id) {
        next.cleanup_run_id = "";
        changed = true;
      }
      if (changed) setPendingArgs(next);
      setOpToConfirm(op);
    }

    function cancelConfirm() {
      setOpToConfirm(null);
    }

    function confirmAndDispatch() {
      const op = opToConfirm;
      if (!op) return;
      const argsMap = Object.assign({}, op.argsMap || {});
      if (op.requiresRunId) {
        argsMap.run_id = (pendingArgs.run_id || "").trim();
        if (!argsMap.run_id) return;
      }
      if (op.requiresCleanupRunId) {
        argsMap.cleanup_run_id = (pendingArgs.cleanup_run_id || "").trim();
        if (!argsMap.cleanup_run_id) return;
      }
      const argsLabel = describeArgs(op, argsMap);
      setOpToConfirm(null);
      jobState.dispatch({
        taskName: op.taskName,
        argsMap: argsMap,
        label: op.label,
        argsLabel: argsLabel,
        onComplete: (finalJob) => {
          // After every successful terminal job, refresh the dashboard so the
          // new totals appear. The two-phase cleanup flow records its proposal
          // separately.
          dashboard.refresh();
          if (
            op.twoPhase &&
            finalJob &&
            finalJob.status !== "CANCELLED" &&
            finalJob.status !== "FAILED"
          ) {
            // The dispatcher emits the dry-run payload via the snapshot, but
            // the token is in the task output. Surface a notice prompting the
            // user to review and execute. The actual token plumbing flows
            // through the unmapped-tags panel (T23); T22 only triggers the
            // dry-run safely.
            setCleanupProposal({
              op_key: op.key,
              generated_at: new Date().toISOString(),
            });
          }
        },
      });
    }

    return h(
      "div",
      { className: "stash-tag-curator-operations" },
      h(
        "p",
        { className: "stash-tag-curator-operations-help" },
        "Each operation runs as a Stash plugin task and is tracked in the job panel below. Destructive operations ask for confirmation first."
      ),
      h(
        "div",
        { className: "stash-tag-curator-op-grid" },
        OPERATIONS.map((op) =>
          h(OperationButton, {
            key: op.key,
            op: op,
            totals: totals,
            jobInProgress: jobInProgress,
            onActivate: () => openConfirm(op),
          })
        )
      ),
      cleanupProposal &&
        h(
          BSAlert,
          { variant: "info", className: "stash-tag-curator-alert" },
          h("strong", null, "Cleanup dry-run finished."),
          " ",
          "A removal proposal was generated. To execute the deletions, open the unmapped-tags / cleanup review panel and confirm there."
        ),
      opToConfirm &&
        h(ConfirmModal, {
          op: opToConfirm,
          totals: totals,
          pendingArgs: pendingArgs,
          setPendingArgs: setPendingArgs,
          onConfirm: confirmAndDispatch,
          onCancel: cancelConfirm,
        })
    );
  }

  function describeArgs(op, argsMap) {
    if (op.requiresRunId) {
      return "run_id=" + String(argsMap.run_id || "");
    }
    if (op.requiresCleanupRunId) {
      return "cleanup_run_id=" + String(argsMap.cleanup_run_id || "");
    }
    if (op.key === "dryRebuild" || op.key === "rebuild") {
      return "dryRun=" + String(argsMap.dryRun);
    }
    return Object.keys(argsMap).length
      ? Object.keys(argsMap)
          .map((k) => k + "=" + String(argsMap[k]))
          .join(", ")
      : "(no args)";
  }

  function OperationButton(props) {
    const op = props.op;
    const totals = props.totals;
    const disabled = !!props.jobInProgress;
    const estimate =
      op.estimateFromTotals && typeof op.estimateFromTotals === "function"
        ? op.estimateFromTotals(totals)
        : null;
    const variant = op.destructive ? "danger" : "primary";

    return h(
      "div",
      { className: "stash-tag-curator-op" },
      h(
        "div",
        { className: "stash-tag-curator-op-label" },
        h("span", { className: "stash-tag-curator-op-name" }, op.label),
        op.destructive
          ? h(
              BSBadge,
              {
                pill: true,
                variant: "danger",
                className: "stash-tag-curator-op-badge",
              },
              "destructive"
            )
          : h(
              BSBadge,
              {
                pill: true,
                variant: "secondary",
                className: "stash-tag-curator-op-badge",
              },
              "safe"
            )
      ),
      h(
        "p",
        { className: "stash-tag-curator-op-scope" },
        op.scope
      ),
      estimate !== null &&
        estimate !== undefined &&
        h(
          "p",
          { className: "stash-tag-curator-op-estimate" },
          "Estimated: ~",
          String(estimate),
          " ",
          op.estimateLabel || ""
        ),
      h(
        BSButton,
        {
          type: "button",
          variant: variant,
          onClick: props.onActivate,
          disabled: disabled,
          className: "stash-tag-curator-op-button",
        },
        op.destructive ? "Review and confirm" : "Run"
      )
    );
  }

  function ConfirmModal(props) {
    const op = props.op;
    const totals = props.totals;
    const pendingArgs = props.pendingArgs;
    const setPendingArgs = props.setPendingArgs;

    const estimate =
      op.estimateFromTotals && typeof op.estimateFromTotals === "function"
        ? op.estimateFromTotals(totals)
        : null;
    const requiresRunId = !!op.requiresRunId;
    const requiresCleanupRunId = !!op.requiresCleanupRunId;
    const runIdValue = (pendingArgs && pendingArgs.run_id) || "";
    const cleanupRunIdValue =
      (pendingArgs && pendingArgs.cleanup_run_id) || "";
    const runIdReady = !requiresRunId || String(runIdValue).trim().length > 0;
    const cleanupRunIdReady =
      !requiresCleanupRunId || String(cleanupRunIdValue).trim().length > 0;
    const ready = runIdReady && cleanupRunIdReady;

    return h(
      ModalShell,
      {
        show: true,
        onHide: props.onCancel,
        title: "Confirm: " + op.label,
        size: "large",
        footer: [
          h(
            BSButton,
            {
              key: "cancel",
              type: "button",
              variant: "secondary",
              onClick: props.onCancel,
            },
            "Cancel"
          ),
          h(
            BSButton,
            {
              key: "confirm",
              type: "button",
              variant: op.destructive ? "danger" : "primary",
              onClick: props.onConfirm,
              disabled: !ready,
            },
            op.destructive ? "Confirm destructive operation" : "Start"
          ),
        ],
      },
      h(
        "p",
        { className: "stash-tag-curator-confirm-scope" },
        op.scope
      ),
      estimate !== null &&
        estimate !== undefined &&
        h(
          "p",
          { className: "stash-tag-curator-confirm-estimate" },
          h("strong", null, "Estimated count:"),
          " ~",
          String(estimate),
          " ",
          op.estimateLabel || ""
        ),
      requiresRunId &&
        h(
          "div",
          { className: "stash-tag-curator-confirm-input" },
          h(
            "p",
            null,
            h(
              "strong",
              null,
              op.key === "rollback"
                ? "Enter the run ID to rollback (find it on the Run History tab):"
                : "Enter the run ID to recover (find it on the Run History tab):"
            )
          ),
          h(
            BSFormGroup,
            null,
            h(BSFormControl, {
              type: "text",
              placeholder: "run id, e.g. rebuild-1a2b3c4d",
              value: runIdValue,
              onChange: (ev) =>
                setPendingArgs(
                  Object.assign({}, pendingArgs, {
                    run_id: (ev && ev.target && ev.target.value) || "",
                  })
                ),
              className: "stash-tag-curator-input-runid",
              "aria-label": "Run ID to rollback",
            })
          )
        ),
      requiresCleanupRunId &&
        h(
          "div",
          { className: "stash-tag-curator-confirm-input" },
          h(
            "p",
            null,
            h(
              "strong",
              null,
              "Enter the cleanup run ID whose deletions should be restored (find it on the Run History tab):"
            )
          ),
          h(
            BSFormGroup,
            null,
            h(BSFormControl, {
              type: "text",
              placeholder: "cleanup run id, e.g. cleanup-safe-1a2b3c4d",
              value: cleanupRunIdValue,
              onChange: (ev) =>
                setPendingArgs(
                  Object.assign({}, pendingArgs, {
                    cleanup_run_id:
                      (ev && ev.target && ev.target.value) || "",
                  })
                ),
              className: "stash-tag-curator-input-cleanup-runid",
              "aria-label": "Cleanup run ID to restore",
            })
          )
        ),
      op.destructive &&
        h(
          BSAlert,
          { variant: "danger", className: "stash-tag-curator-alert" },
          h("strong", null, "This operation is destructive."),
          " Scene tag sets will be mutated. Each mutation is journaled and reversible via the rollback panel, but review the scope carefully before confirming."
        ),
      op.twoPhase &&
        h(
          BSAlert,
          { variant: "info", className: "stash-tag-curator-alert" },
          "This button triggers the dry-run phase only. Tag deletions require a second confirmation after the proposal is reviewed."
        )
    );
  }

  // ------------------------------------------------------------------
  // Job status panel
  // ------------------------------------------------------------------

  function JobPanel(props) {
    const jobState = props.jobState;
    const job = jobState.job;
    if (!job && !jobState.dispatchError && !jobState.pollError) {
      return null;
    }
    const status = job ? String(job.status || "").toUpperCase() : null;
    const inProgress = job && !isTerminalStatus(status);
    const pct = job ? formatPercent(job.progress) : "0%";

    return h(
      "section",
      { className: "stash-tag-curator-job-panel" },
      h("h4", null, "Active job"),
      jobState.dispatchError &&
        h(
          BSAlert,
          { variant: "danger", className: "stash-tag-curator-alert" },
          String(jobState.dispatchError)
        ),
      jobState.pollError &&
        h(
          BSAlert,
          { variant: "warning", className: "stash-tag-curator-alert" },
          String(jobState.pollError)
        ),
      job &&
        h(
          "div",
          { className: "stash-tag-curator-job-detail" },
          h(
            "div",
            { className: "stash-tag-curator-job-row" },
            h("span", { className: "stash-tag-curator-muted" }, "Task: "),
            h("span", null, job.label || job.task_name || "(unknown)"),
            status &&
              h(
                BSBadge,
                {
                  pill: true,
                  variant: badgeVariantForStatus(status),
                  className: "stash-tag-curator-job-badge",
                },
                status
              )
          ),
          job.args_label &&
            h(
              "div",
              { className: "stash-tag-curator-job-row" },
              h("span", { className: "stash-tag-curator-muted" }, "Args: "),
              h(
                "span",
                { className: "stash-tag-curator-mono" },
                String(job.args_label)
              )
            ),
          h(
            "div",
            { className: "stash-tag-curator-job-row" },
            h("span", { className: "stash-tag-curator-muted" }, "Job ID: "),
            h(
              "span",
              { className: "stash-tag-curator-mono" },
              String(job.job_id)
            )
          ),
          inProgress &&
            h(
              "div",
              { className: "stash-tag-curator-progress" },
              h("div", { className: "stash-tag-curator-progress-track" },
                h("div", {
                  className: "stash-tag-curator-progress-fill",
                  style: { width: pct },
                })
              ),
              h("span", { className: "stash-tag-curator-progress-label" }, pct)
            ),
          job.error &&
            h(
              "div",
              { className: "stash-tag-curator-job-row stash-tag-curator-error" },
              h("span", { className: "stash-tag-curator-muted" }, "Error: "),
              String(job.error)
            ),
          h(
            "div",
            { className: "stash-tag-curator-job-actions" },
            inProgress
              ? h(
                  BSButton,
                  {
                    type: "button",
                    size: "sm",
                    variant: "danger",
                    onClick: jobState.cancel,
                  },
                  "Cancel (SIGKILL)"
                )
              : h(
                  BSButton,
                  {
                    type: "button",
                    size: "sm",
                    variant: "secondary",
                    onClick: jobState.dismiss,
                  },
                  "Dismiss"
                )
          )
        )
    );
  }

  function badgeVariantForStatus(status) {
    const s = String(status || "").toUpperCase();
    if (s === "COMPLETE" || s === "COMPLETED") return "success";
    if (s === "FAILED" || s === "FAILURE") return "danger";
    if (s === "CANCELLED" || s === "CANCELED" || s === "STOPPED") return "secondary";
    if (s === "REMOVED") return "dark";
    return "info";
  }

  // ------------------------------------------------------------------
  // Generic asset-snapshot hook (T23: unmapped_tags / run_history / rules_audit)
  // ------------------------------------------------------------------

  function useAssetSnapshot(name, autoRefresh) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [lastUpdated, setLastUpdated] = useState(null);
    const timerRef = useRef(null);
    const nameRef = useRef(name);
    nameRef.current = name;

    const fetchOnce = useCallback(async function fetchOnce() {
      try {
        const url =
          ASSET_BASE +
          nameRef.current +
          ".json?_=" +
          Date.now();
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error("asset HTTP " + response.status);
        }
        const snapshot = await response.json();
        setData(snapshot);
        setError(null);
        setLastUpdated(new Date().toISOString());
      } catch (err) {
        setError(err && err.message ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    }, []);

    const refresh = useCallback(() => {
      fetchOnce();
    }, [fetchOnce]);

    useEffect(() => {
      setData(null);
      setError(null);
      setLoading(true);
      fetchOnce();
    }, [name, fetchOnce]);

    useEffect(() => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (autoRefresh) {
        const tick = () => {
          fetchOnce().finally(() => {
            timerRef.current = setTimeout(tick, DASHBOARD_REFRESH_MS);
          });
        };
        timerRef.current = setTimeout(tick, DASHBOARD_REFRESH_MS);
      }
      return () => {
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
      };
    }, [autoRefresh, fetchOnce]);

    return {
      data: data,
      error: error,
      loading: loading,
      lastUpdated: lastUpdated,
      refresh: refresh,
    };
  }

  // ------------------------------------------------------------------
  // Cancel helper: find the active curator job (via jobQueue) and stopJob it.
  // Used by the Run History cancel button when jobState has no active job
  // (e.g. the run was started from another UI session or a Stash task menu).
  // ------------------------------------------------------------------

  const JOB_QUEUE_QUERY = `
    query CuratorJobQueue {
      jobQueue {
        id
        status
        description
        progress
        subTasks
      }
    }
  `;

  async function findActiveCuratorJobId() {
    try {
      const data = await gql(JOB_QUEUE_QUERY, {});
      const queue = (data && data.jobQueue) || [];
      const running = queue.find((j) => {
        if (!j) return false;
        const status = String(j.status || "").toUpperCase();
        if (
          status === "READY" ||
          status === "RUNNING" ||
          status === "IMPORTING" ||
          status === "QUEUED" ||
          status === "STALLED"
        ) {
          const desc = String(j.description || j.subTasks || "");
          return desc.toLowerCase().indexOf("curator") >= 0 || desc.toLowerCase().indexOf("tag") >= 0;
        }
        return false;
      });
      return running ? running.id : null;
    } catch (err) {
      return null;
    }
  }

  // ------------------------------------------------------------------
  // Unmapped-tags review queue panel (T23 part 1)
  // ------------------------------------------------------------------

  // Disposition radio options. The four v3 dispositions map 1:1 to the
  // buttons. "detail" is pass-through (output = the raw tag itself);
  // "ignore" forbids outputs; "defer" carries optional notes; "map"
  // requires user-provided canonical outputs.
  const UNMAPPED_DISPOSITIONS = [
    { value: "map", label: "Map", needsOutputs: true },
    { value: "detail", label: "Pass-through (detail)", needsOutputs: false },
    { value: "ignore", label: "Ignore / blacklist", needsOutputs: false },
    { value: "defer", label: "Defer", needsOutputs: false },
  ];

  function emptyEdit() {
    return { disposition: null, outputs: [], notes: "" };
  }

  function UnmappedTagsPanel(props) {
    const snapshot = useAssetSnapshot("unmapped_tags", !!props.autoRefresh);
    const jobState = props.jobState;
    const [query, setQuery] = useState("");
    const [sortKey, setSortKey] = useState("occurrence_count");
    const [sortDesc, setSortDesc] = useState(true);
    const [pending, setPending] = useState({}); // {raw_tag -> edit}
    const [saveError, setSaveError] = useState(null);
    const [saveResultError, setSaveResultError] = useState(null);
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [conflictOpen, setConflictOpen] = useState(false);
    const expectedShaRef = useRef(null);

    const rulesChecksum =
      (snapshot.data && snapshot.data.rules_checksum) || null;
    const tags = (snapshot.data && snapshot.data.tags) || [];
    const totalUnmapped = (snapshot.data && snapshot.data.total_unmapped) || 0;
    const jobInProgress = !!(jobState.job && !isTerminalStatus(jobState.job.status));
    const pendingCount = Object.keys(pending).length;

    function setDisposition(rawTag, disposition) {
      setPending((prev) => {
        const next = Object.assign({}, prev);
        const existing = next[rawTag] || emptyEdit();
        next[rawTag] = Object.assign({}, existing, {
          disposition: disposition,
        });
        return next;
      });
    }

    function addOutput(rawTag, value) {
      const trimmed = String(value || "").trim();
      if (!trimmed) return;
      setPending((prev) => {
        const next = Object.assign({}, prev);
        const existing = next[rawTag] || emptyEdit();
        const outputs = (existing.outputs || []).slice();
        if (outputs.indexOf(trimmed) === -1) {
          outputs.push(trimmed);
        }
        next[rawTag] = Object.assign({}, existing, {
          disposition: existing.disposition || "map",
          outputs: outputs,
        });
        return next;
      });
    }

    function removeOutput(rawTag, value) {
      setPending((prev) => {
        const next = Object.assign({}, prev);
        const existing = next[rawTag];
        if (!existing) return prev;
        const outputs = (existing.outputs || []).filter((o) => o !== value);
        next[rawTag] = Object.assign({}, existing, { outputs: outputs });
        return next;
      });
    }

    function setNotes(rawTag, notes) {
      setPending((prev) => {
        const next = Object.assign({}, prev);
        const existing = next[rawTag] || emptyEdit();
        next[rawTag] = Object.assign({}, existing, { notes: notes });
        return next;
      });
    }

    function clearRow(rawTag) {
      setPending((prev) => {
        const next = Object.assign({}, prev);
        delete next[rawTag];
        return next;
      });
    }

    function clearAll() {
      setPending({});
      setSaveError(null);
    }

    // Filter + sort. Search matches raw_tag, display_form, or notes.
    const visibleTags = useMemo(() => {
      const q = query.trim().toLowerCase();
      let rows = tags;
      if (q) {
        rows = rows.filter((t) => {
          const raw = String(t.raw_tag || "").toLowerCase();
          const display = String(t.display_form || "").toLowerCase();
          const notes = String(t.notes || "").toLowerCase();
          return (
            raw.indexOf(q) >= 0 ||
            display.indexOf(q) >= 0 ||
            notes.indexOf(q) >= 0
          );
        });
      }
      const sorted = rows.slice().sort((a, b) => {
        let av = a[sortKey];
        let bv = b[sortKey];
        if (sortKey === "raw_tag") {
          av = String(av || "").toLowerCase();
          bv = String(bv || "").toLowerCase();
        } else {
          av = Number(av || 0);
          bv = Number(bv || 0);
        }
        if (av < bv) return sortDesc ? 1 : -1;
        if (av > bv) return sortDesc ? -1 : 1;
        return 0;
      });
      return sorted;
    }, [tags, query, sortKey, sortDesc]);

    function toggleSort(key) {
      if (sortKey === key) {
        setSortDesc(!sortDesc);
      } else {
        setSortKey(key);
        setSortDesc(true);
      }
    }

    function buildSavePayload() {
      const changes = Object.keys(pending).map((rawTag) => {
        const edit = pending[rawTag];
        const disposition = edit.disposition || "defer";
        const item = {
          normalized_key: rawTag,
          disposition: disposition,
        };
        if (disposition === "detail") {
          item.outputs = [rawTag];
        } else if (disposition === "map") {
          item.outputs = (edit.outputs || []).slice();
        }
        // ignore omits outputs; defer may omit outputs or carry existing ones.
        if (edit.notes) {
          item.notes = edit.notes;
        }
        return item;
      });
      return {
        expected_rules_sha: rulesChecksum,
        changes: changes,
        canonical_additions: [],
      };
    }

    function openSaveConfirm() {
      setSaveError(null);
      if (pendingCount === 0) {
        setSaveError("No pending changes to save.");
        return;
      }
      if (!rulesChecksum) {
        setSaveError(
          "Cannot save: the unmapped-tags snapshot has no rules checksum. Reload the panel."
        );
        return;
      }
      setConfirmOpen(true);
    }

    async function fetchRulesAuditChecksum() {
      const url = ASSET_BASE + "rules_audit.json?_=" + Date.now();
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("rules audit asset HTTP " + response.status);
      }
      const data = await response.json();
      return data && data.rules_checksum;
    }

    function confirmAndSave() {
      const payload = buildSavePayload();
      const expectedSha = rulesChecksum;
      expectedShaRef.current = expectedSha;
      const argsMap = {
        expected_rules_sha: expectedSha,
        changes: payload.changes,
        canonical_additions: payload.canonical_additions,
      };
      setConfirmOpen(false);
      setSaveError(null);
      setSaveResultError(null);
      setConflictOpen(false);
      jobState.dispatch({
        taskName: "SaveMapping",
        argsMap: argsMap,
        label: "Save Mapping Edit",
        argsLabel: pendingCount + " change" + (pendingCount === 1 ? "" : "s"),
        onComplete: (finalJob) => verifySaveResult(finalJob, expectedSha),
      });
    }

    async function verifySaveResult(finalJob, expectedSha) {
      if (!finalJob) return;
      const status = String(finalJob.status || "").toUpperCase();
      if (status === "FAILED" || status === "CANCELLED" || status === "REMOVED") {
        const errMsg = finalJob.error || status;
        if (String(errMsg).toLowerCase().indexOf("rules_changed") >= 0) {
          setConflictOpen(true);
        } else {
          setSaveResultError("Save failed: " + String(errMsg));
        }
        return;
      }
      try {
        const freshChecksum = await fetchRulesAuditChecksum();
        if (freshChecksum && freshChecksum !== expectedSha) {
          // Backend regenerated snapshots with a new checksum = success.
          snapshot.refresh();
          if (props.onRulesAuditRefresh) props.onRulesAuditRefresh();
          if (props.onRulesChanged) props.onRulesChanged();
          setPending({});
          setSaveResultError(null);
          setConflictOpen(false);
        } else {
          // Checksum did not change: rules_changed (or validation failure).
          setConflictOpen(true);
        }
      } catch (err) {
        setSaveResultError(
          "Could not verify save result: " +
            (err && err.message ? err.message : String(err))
        );
      }
    }

    function reloadAndReapply() {
      setConflictOpen(false);
      setSaveResultError(null);
      snapshot.refresh();
      if (props.onRulesAuditRefresh) props.onRulesAuditRefresh();
      if (props.onRulesChanged) props.onRulesChanged();
    }

    return h(
      "div",
      { className: "stash-tag-curator-unmapped" },
      h(
        "div",
        { className: "stash-tag-curator-unmapped-toolbar" },
        h(
          "div",
          { className: "stash-tag-curator-search" },
          h(BSFormControl, {
            type: "search",
            placeholder: "Search raw tags, display form, notes...",
            value: query,
            onChange: (ev) => setQuery((ev && ev.target && ev.target.value) || ""),
            "aria-label": "Filter unmapped tags",
            className: "stash-tag-curator-input-search",
          })
        ),
        h(
          "div",
          { className: "stash-tag-curator-unmapped-actions" },
          h(
            "span",
            { className: "stash-tag-curator-muted" },
            totalUnmapped + " unmapped / " + tags.length + " shown"
          ),
          h(
            BSButton,
            {
              type: "button",
              size: "sm",
              variant: "secondary",
              onClick: snapshot.refresh,
              disabled: snapshot.loading,
            },
            snapshot.loading ? "Refreshing..." : "Refresh"
          ),
          h(
            BSButton,
            {
              type: "button",
              size: "sm",
              variant: "outline-secondary",
              onClick: clearAll,
              disabled: pendingCount === 0 || jobInProgress,
            },
            "Clear pending"
          ),
          h(
            BSButton,
            {
              type: "button",
              size: "sm",
              variant: "primary",
              onClick: openSaveConfirm,
              disabled: pendingCount === 0 || jobInProgress || !rulesChecksum,
              className: "stash-tag-curator-save-mapping",
            },
            "Save Mapping (" + pendingCount + ")"
          )
        )
      ),
      snapshot.error &&
        h(
          BSAlert,
          { variant: "warning", className: "stash-tag-curator-alert" },
          "Snapshot unavailable: ",
          String(snapshot.error),
          h("br", null),
          "Run the Unmapped Tags report task or any mutation task to generate it."
        ),
      saveError &&
        h(
          BSAlert,
          { variant: "danger", className: "stash-tag-curator-alert" },
          String(saveError)
        ),
      saveResultError &&
        h(
          BSAlert,
          { variant: "danger", className: "stash-tag-curator-alert" },
          String(saveResultError)
        ),
      conflictOpen &&
        h(MappingConflictModal, {
          onClose: () => setConflictOpen(false),
          onReload: reloadAndReapply,
        }),
      jobInProgress &&
        h(
          BSAlert,
          { variant: "info", className: "stash-tag-curator-alert" },
          "A curator job is currently running. Save is disabled until it completes (D17: rules edits are prohibited while a run is locked)."
        ),
      rulesChecksum &&
        h(
          "div",
          { className: "stash-tag-curator-unmapped-checksum" },
          h("span", { className: "stash-tag-curator-muted" }, "Rules checksum: "),
          h(
            "span",
            { className: "stash-tag-curator-mono stash-tag-curator-mono-trunc" },
            rulesChecksum
          )
        ),
      tags.length === 0 && !snapshot.loading
        ? h(
            "p",
            { className: "stash-tag-curator-muted" },
            "No unmapped tags found. Either every raw tag resolves through the active rules, or the snapshot has not been generated yet."
          )
        : h(
            "div",
            { className: "stash-tag-curator-table-scroll" },
            h(
              "table",
              { className: "stash-tag-curator-unmapped-table" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  h(
                    "th",
                    {
                      className: "stash-tag-curator-th-sortable",
                      onClick: () => toggleSort("raw_tag"),
                    },
                    "Raw tag",
                    sortKey === "raw_tag" ? (sortDesc ? " \u2193" : " \u2191") : ""
                  ),
                  h(
                    "th",
                    {
                      className: "stash-tag-curator-th-sortable",
                      onClick: () => toggleSort("occurrence_count"),
                    },
                    "Scenes",
                    sortKey === "occurrence_count" ? (sortDesc ? " \u2193" : " \u2191") : ""
                  ),
                  h("th", null, "Disposition"),
                  h("th", null, "Outputs"),
                  h("th", null, "Notes"),
                  h("th", null, "Actions")
                )
              ),
              h(
                "tbody",
                null,
                visibleTags.map((row) =>
                  h(UnmappedRow, {
                    key: row.raw_tag,
                    row: row,
                    edit: pending[row.raw_tag] || null,
                    onSetDisposition: setDisposition,
                    onAddOutput: addOutput,
                    onRemoveOutput: removeOutput,
                    onSetNotes: setNotes,
                    onClear: clearRow,
                  })
                )
              )
            )
          ),
      confirmOpen &&
        h(SaveMappingConfirmModal, {
          pendingCount: pendingCount,
          rulesChecksum: rulesChecksum,
          onConfirm: confirmAndSave,
          onCancel: () => setConfirmOpen(false),
        })
    );
  }

  function UnmappedRow(props) {
    const row = props.row;
    const rawTag = String(row.raw_tag || "");
    const edit = props.edit || emptyEdit();
    const disposition = edit.disposition || null;
    const [outputDraft, setOutputDraft] = useState("");

    return h(
      "tr",
      { className: "stash-tag-curator-unmapped-row" },
      h(
        "td",
        null,
        h("div", { className: "stash-tag-curator-rawtag" }, rawTag),
        row.display_form && row.display_form !== rawTag
          ? h(
              "div",
              { className: "stash-tag-curator-muted stash-tag-curator-displayform" },
              String(row.display_form)
            )
          : null,
        row.first_seen || row.last_seen
          ? h(
              "div",
              { className: "stash-tag-curator-muted stash-tag-curator-seen" },
              "seen ",
              formatTimestamp(row.first_seen) || "?",
              " \u2192 ",
              formatTimestamp(row.last_seen) || "?"
            )
          : null
      ),
      h("td", null, String(row.occurrence_count || 0)),
      h(
        "td",
        null,
        UNMAPPED_DISPOSITIONS.map((opt) =>
          h(
            "label",
            {
              key: opt.value,
              className: "stash-tag-curator-disposition-option",
            },
            h("input", {
              type: "radio",
              name: "disp-" + rawTag,
              value: opt.value,
              checked: disposition === opt.value,
              onChange: () => props.onSetDisposition(rawTag, opt.value),
            }),
            " ",
            opt.label
          )
        )
      ),
      h(
        "td",
        null,
        disposition === "map"
          ? h(MapOutputsCell, {
              rawTag: rawTag,
              outputs: edit.outputs || [],
              draft: outputDraft,
              setDraft: setOutputDraft,
              onAdd: () => {
                props.onAddOutput(rawTag, outputDraft);
                setOutputDraft("");
              },
              onRemove: (val) => props.onRemoveOutput(rawTag, val),
            })
          : disposition === "detail"
          ? h(
              "span",
              { className: "stash-tag-curator-muted" },
              "Pass-through: ",
              h("code", null, rawTag)
            )
          : disposition === "ignore"
          ? h(
              "span",
              { className: "stash-tag-curator-muted" },
              "(no outputs)"
            )
          : disposition === "defer"
          ? h(
              "span",
              { className: "stash-tag-curator-muted" },
              "(deferred for review)"
            )
          : h("span", { className: "stash-tag-curator-muted" }, "\u2014")
      ),
      h(
        "td",
        null,
        h(BSFormControl, {
          type: "text",
          as: "input",
          value: edit.notes || "",
          onChange: (ev) =>
            props.onSetNotes(rawTag, (ev && ev.target && ev.target.value) || ""),
          placeholder: "optional",
          className: "stash-tag-curator-input-notes",
          "aria-label": "Notes for " + rawTag,
        })
      ),
      h(
        "td",
        null,
        h(
          BSButton,
          {
            type: "button",
            size: "sm",
            variant: "outline-secondary",
            onClick: () => props.onClear(rawTag),
            disabled: !disposition,
          },
          "Reset"
        )
      )
    );
  }

  // MapOutputsCell: a multi-select control built from a text input + Add
  // button + chip list. The user can attach multiple canonical tag names to
  // a single raw tag. T32 may swap this for a ReactSelect multi-dropdown
  // populated from the canonical set; this minimal control satisfies the
  // "multi-select (not single dropdown)" QA requirement.
  function MapOutputsCell(props) {
    const outputs = props.outputs || [];
    return h(
      "div",
      { className: "stash-tag-curator-map-outputs" },
      h(
        "div",
        { className: "stash-tag-curator-map-input-row" },
        h(BSFormControl, {
          type: "text",
          value: props.draft,
          onChange: (ev) => props.setDraft((ev && ev.target && ev.target.value) || ""),
          placeholder: "canonical tag (e.g. KINK: Roleplay)",
          className: "stash-tag-curator-input-canonical",
          "aria-label": "Add canonical output for " + props.rawTag,
          onKeyDown: (ev) => {
            if (ev && ev.key === "Enter") {
              ev.preventDefault();
              props.onAdd();
            }
          },
        }),
        h(
          BSButton,
          {
            type: "button",
            size: "sm",
            variant: "secondary",
            onClick: props.onAdd,
            disabled: !String(props.draft || "").trim(),
            className: "stash-tag-curator-add-output",
          },
          "Add"
        )
      ),
      outputs.length > 0
        ? h(
            "ul",
            { className: "stash-tag-curator-output-chips stash-tag-curator-multiselect-list" },
            outputs.map((out) =>
              h(
                "li",
                { key: out, className: "stash-tag-curator-output-chip" },
                h("code", null, out),
                " ",
                h(
                  "button",
                  {
                    type: "button",
                    className: "stash-tag-curator-chip-remove",
                    onClick: () => props.onRemove(out),
                    "aria-label": "Remove " + out,
                  },
                  "\u00d7"
                )
              )
            )
          )
        : h(
            "span",
            { className: "stash-tag-curator-muted stash-tag-curator-no-outputs" },
            "(add at least one canonical tag)"
          )
    );
  }

  function SaveMappingConfirmModal(props) {
    return h(
      ModalShell,
      {
        show: true,
        title: "Confirm mapping edit",
        onConfirm: props.onConfirm,
        onCancel: props.onCancel,
        size: "large",
        footer: [
          h(
            BSButton,
            { key: "cancel", type: "button", variant: "secondary", onClick: props.onCancel },
            "Cancel"
          ),
          h(
            BSButton,
            {
              key: "confirm",
              type: "button",
              variant: "primary",
              onClick: props.onConfirm,
            },
            "Save " + props.pendingCount + " change" + (props.pendingCount === 1 ? "" : "s")
          ),
        ],
      },
      h(
        "p",
        null,
        "You are about to persist ",
        h("strong", null, String(props.pendingCount)),
        " mapping ",
        props.pendingCount === 1 ? "change" : "changes",
        " to the active rules file."
      ),
      h(
        "p",
        null,
        "The edit uses optimistic concurrency: if the rules checksum has changed since you opened this panel, the backend will reject the save (",
        h("code", null, "rules_changed"),
        ") and you will need to reload and re-apply."
      ),
      h(
        "p",
        null,
        h("span", { className: "stash-tag-curator-muted" }, "Expected rules checksum: "),
        h(
          "span",
          { className: "stash-tag-curator-mono stash-tag-curator-mono-trunc" },
          props.rulesChecksum || "\u2014"
        )
      )
    );
  }
  function MappingConflictModal(props) {
    return h(
      ModalShell,
      {
        show: true,
        title: "Rules changed",
        onHide: props.onClose,
        size: "large",
        footer: [
          h(
            BSButton,
            { key: "close", type: "button", variant: "secondary", onClick: props.onClose },
            "Close"
          ),
          h(
            BSButton,
            {
              key: "reload",
              type: "button",
              variant: "primary",
              onClick: props.onReload,
            },
            "Reload and re-apply"
          ),
        ],
      },
      h(
        "p",
        null,
        "The active rules file has changed since you opened this panel. The save was not applied."
      ),
      h(
        "p",
        null,
        "Click ",
        h("strong", null, "Reload and re-apply"),
        " to refresh the snapshots and keep your pending changes. Review the updated checksum, then save again."
      )
    );
  }

  // ------------------------------------------------------------------
  // Run history panel (T23 part 2)
  // ------------------------------------------------------------------

  function RunHistoryPanel(props) {
    const snapshot = useAssetSnapshot("run_history", !!props.autoRefresh);
    const jobState = props.jobState;
    const [rollbackTarget, setRollbackTarget] = useState(null);
    const [cancelTarget, setCancelTarget] = useState(null);
    const [cancelError, setCancelError] = useState(null);

    const runs = (snapshot.data && snapshot.data.runs) || [];

    async function stopActiveRun() {
      setCancelError(null);
      let jobId = null;
      // Prefer the active job tracked by jobState (same UI session).
      if (jobState.job && !isTerminalStatus(jobState.job.status)) {
        jobId = jobState.job.job_id;
      } else {
        // Page reloaded or run started elsewhere: query the queue.
        jobId = await findActiveCuratorJobId();
      }
      if (!jobId) {
        setCancelError(
          "Could not find an active curator job to cancel. It may have already finished."
        );
        return;
      }
      try {
        await gql(STOP_JOB_MUTATION, { job_id: jobId });
        // The job panel will reflect terminal status on the next poll.
        if (jobState.job && jobState.job.job_id === jobId) {
          jobState.cancel();
        }
        snapshot.refresh();
      } catch (err) {
        setCancelError(
          "Cancel failed: " + (err && err.message ? err.message : String(err))
        );
      }
    }

    function triggerRollback(runRow) {
      setRollbackTarget(runRow);
    }

    function confirmRollback() {
      const target = rollbackTarget;
      if (!target) return;
      const runId = String(target.run_id || "").trim();
      if (!runId) {
        setRollbackTarget(null);
        return;
      }
      setRollbackTarget(null);
      jobState.dispatch({
        taskName: "Rollback",
        argsMap: { run_id: runId, policy: "skip-with-warning" },
        label: "Rollback run " + runId,
        argsLabel: "run_id=" + runId,
        onComplete: () => {
          snapshot.refresh();
          if (props.onRunChanged) props.onRunChanged();
        },
      });
    }

    function triggerCancel(runRow) {
      setCancelError(null);
      setCancelTarget(runRow);
    }

    function confirmCancel() {
      const target = cancelTarget;
      if (!target) return;
      setCancelTarget(null);
      stopActiveRun();
    }

    function onRecoveryAction(run, action) {
      const runId = String((run && run.run_id) || "").trim();
      if (!runId || !action) return;
      const taskNameByAction = {
        resume_run: "ResumeRun",
        abandon_run: "AbandonRun",
        force_release: "ForceRelease",
      };
      const taskName = taskNameByAction[action];
      if (!taskName) return;
      const labelByAction = {
        resume_run: "Resume run " + runId,
        abandon_run: "Abandon run " + runId,
        force_release: "Force-release lock for run " + runId,
      };
      jobState.dispatch({
        taskName: taskName,
        argsMap: { run_id: runId },
        label: labelByAction[action],
        argsLabel: "run_id=" + runId,
        onComplete: () => {
          snapshot.refresh();
          if (props.onRunChanged) props.onRunChanged();
        },
      });
    }

    return h(
      "div",
      { className: "stash-tag-curator-runhistory" },
      h(
        "div",
        { className: "stash-tag-curator-runhistory-toolbar" },
        h(
          BSButton,
          {
            type: "button",
            size: "sm",
            variant: "secondary",
            onClick: snapshot.refresh,
            disabled: snapshot.loading,
          },
          snapshot.loading ? "Refreshing..." : "Refresh"
        )
      ),
      cancelError &&
        h(
          BSAlert,
          { variant: "warning", className: "stash-tag-curator-alert" },
          String(cancelError)
        ),
      snapshot.error &&
        h(
          BSAlert,
          { variant: "warning", className: "stash-tag-curator-alert" },
          "Run history unavailable: ",
          String(snapshot.error),
          h("br", null),
          "Run the Run History report task or any mutation task to generate it."
        ),
      runs.length === 0 && !snapshot.loading
        ? h(
            "p",
            { className: "stash-tag-curator-muted" },
            "No runs recorded yet. Once you run an operation, its lifecycle row appears here."
          )
        : h(
            "div",
            { className: "stash-tag-curator-table-scroll" },
            h(
              "table",
              { className: "stash-tag-curator-runhistory-table" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  h("th", null, "Operation"),
                  h("th", null, "Status"),
                  h("th", null, "Started"),
                  h("th", null, "Ended"),
                  h("th", null, "Scope"),
                  h("th", null, "Changed"),
                  h("th", null, "Skipped"),
                  h("th", null, "Failed"),
                  h("th", null, "Unmapped"),
                  h("th", null, "Rules"),
                  h("th", null, "Run ID"),
                  h("th", null, "Actions")
                )
              ),
              h(
                "tbody",
                null,
                runs.map((run) =>
                  h(RunHistoryRow, {
                    key: run.run_id,
                    run: run,
                    onRollback: triggerRollback,
                    onCancel: triggerCancel,
                    onRecoveryAction: onRecoveryAction,
                  })
                )
              )
            )
          ),
      rollbackTarget &&
        h(RollbackConfirmModal, {
          run: rollbackTarget,
          onConfirm: confirmRollback,
          onCancel: () => setRollbackTarget(null),
        }),
      cancelTarget &&
        h(StopRunConfirmModal, {
          run: cancelTarget,
          onConfirm: confirmCancel,
          onCancel: () => setCancelTarget(null),
        })
    );
  }

  function isRunningStatus(status) {
    const s = String(status || "").toLowerCase();
    return (
      s === "running" ||
      s === "active" ||
      s === "in_progress" ||
      s === "ready" ||
      s === "queued"
    );
  }

  function RunHistoryRow(props) {
    const run = props.run;
    const status = String(run.status || "");
    const running = isRunningStatus(status);
    const canRollback = !!run.rollback_available && !running;
    const shortChecksum = String(run.rules_sha || "").substring(0, 12);

    return h(
      "tr",
      { className: "stash-tag-curator-runhistory-row" + (running ? " stash-tag-curator-runhistory-row-running" : "") },
      h("td", null, String(run.operation || "\u2014")),
      h(
        "td",
        null,
        h(
          BSBadge,
          {
            pill: true,
            variant: badgeVariantForStatus(status),
            className: "stash-tag-curator-status-badge",
          },
          status || "\u2014"
        )
      ),
      h("td", null, formatTimestamp(run.started_at) || "\u2014"),
      h("td", null, formatTimestamp(run.ended_at) || "\u2014"),
      h("td", null, String(run.scope || "\u2014")),
      h("td", null, String(run.scenes_changed || 0)),
      h("td", null, String(run.scenes_skipped || 0)),
      h("td", null, String(run.failures || 0)),
      h("td", null, run.unmapped_count != null ? String(run.unmapped_count) : "\u2014"),
      h(
        "td",
        { className: "stash-tag-curator-mono", title: run.rules_sha || "" },
        shortChecksum || "\u2014"
      ),
      h(
        "td",
        { className: "stash-tag-curator-mono stash-tag-curator-runid-cell", title: run.run_id || "" },
        String(run.run_id || "\u2014")
      ),
      h(
        "td",
        { className: "stash-tag-curator-runhistory-actions" },
        (function () {
          const cells = [];
          if (running) {
            cells.push(
              h(
                BSButton,
                {
                  key: "cancel",
                  type: "button",
                  size: "sm",
                  variant: "danger",
                  onClick: () => props.onCancel(run),
                  className: "stash-tag-curator-cancel-run",
                },
                "Cancel"
              )
            );
          } else {
            if (canRollback) {
              cells.push(
                h(
                  BSButton,
                  {
                    key: "rollback",
                    type: "button",
                    size: "sm",
                    variant: "outline-danger",
                    onClick: () => props.onRollback(run),
                    className: "stash-tag-curator-rollback-run",
                  },
                  "Rollback"
                )
              );
            }
            if (
              run.run_id &&
              typeof props.onRecoveryAction === "function"
            ) {
              cells.push(
                h(
                  BSButton,
                  {
                    key: "resume",
                    type: "button",
                    size: "sm",
                    variant: "outline-secondary",
                    onClick: () => props.onRecoveryAction(run, "resume_run"),
                    className: "stash-tag-curator-recover-resume",
                  },
                  "Resume"
                ),
                " ",
                h(
                  BSButton,
                  {
                    key: "abandon",
                    type: "button",
                    size: "sm",
                    variant: "outline-warning",
                    onClick: () => props.onRecoveryAction(run, "abandon_run"),
                    className: "stash-tag-curator-recover-abandon",
                  },
                  "Abandon"
                ),
                " ",
                h(
                  BSButton,
                  {
                    key: "force",
                    type: "button",
                    size: "sm",
                    variant: "outline-danger",
                    onClick: () => props.onRecoveryAction(run, "force_release"),
                    className: "stash-tag-curator-recover-force",
                  },
                  "Force Release"
                )
              );
            }
            if (cells.length === 0) {
              cells.push(
                h(
                  "span",
                  { key: "none", className: "stash-tag-curator-muted" },
                  "\u2014"
                )
              );
            }
          }
          return cells;
        })()
      )
    );
  }

  function RollbackConfirmModal(props) {
    const run = props.run;
    return h(
      ModalShell,
      {
        show: true,
        title: "Confirm rollback: " + (run.operation || "run"),
        onConfirm: props.onConfirm,
        onCancel: props.onCancel,
        size: "large",
        footer: [
          h(
            BSButton,
            { key: "cancel", type: "button", variant: "secondary", onClick: props.onCancel },
            "Cancel"
          ),
          h(
            BSButton,
            {
              key: "confirm",
              type: "button",
              variant: "danger",
              onClick: props.onConfirm,
            },
            "Confirm rollback"
          ),
        ],
      },
      h(
        "p",
        null,
        "This reverts the effects of run ",
        h("code", null, String(run.run_id || "")),
        ". Each scene mutated by this run will have its tag set restored to the recorded pre-run state."
      ),
      h(
        "p",
        null,
        h("strong", null, "Scope:"),
        " ",
        String(run.scenes_changed || 0),
        " scene(s) changed, ",
        String(run.scenes_skipped || 0),
        " skipped, ",
        String(run.failures || 0),
        " failure(s)."
      ),
      h(
        BSAlert,
        { variant: "warning", className: "stash-tag-curator-alert" },
        h("strong", null, "Conflict policy:"),
        " skip-with-warning. Scenes modified after this run (by a later run or by manual edits) will be skipped and listed in the rollback report."
      )
    );
  }

  function StopRunConfirmModal(props) {
    const run = props.run;
    return h(
      ModalShell,
      {
        show: true,
        title: "Cancel running job?",
        onConfirm: props.onConfirm,
        onCancel: props.onCancel,
        footer: [
          h(
            BSButton,
            { key: "cancel", type: "button", variant: "secondary", onClick: props.onCancel },
            "Keep running"
          ),
          h(
            BSButton,
            {
              key: "confirm",
              type: "button",
              variant: "danger",
              onClick: props.onConfirm,
            },
            "Cancel job (SIGKILL)"
          ),
        ],
      },
      h(
        BSAlert,
        { variant: "danger", className: "stash-tag-curator-alert" },
        h("strong", null, "Stash sends SIGKILL - there is no graceful cancel."),
        h("br", null),
        "The Python process is terminated immediately; the run lock goes stale and must be force-released (or resumed) from the dashboard before a new run can start. Checkpoint and journal state is preserved (D5/D16/D21)."
      ),
      h(
        "p",
        null,
        "Target: ",
        h("code", null, String(run.run_id || "")),
        " (",
        String(run.operation || ""),
        ")"
      )
    );
  }

  // ------------------------------------------------------------------
  // Rules audit panel (T23 part 3)
  // ------------------------------------------------------------------

  function RulesAuditPanel(props) {
    const snapshot = props.snapshot || {};
    const data = snapshot.data;

    return h(
      "div",
      { className: "stash-tag-curator-rulesaudit" },
      h(
        "div",
        { className: "stash-tag-curator-rulesaudit-toolbar" },
        h(
          BSButton,
          {
            type: "button",
            size: "sm",
            variant: "secondary",
            onClick: snapshot.refresh,
            disabled: snapshot.loading,
          },
          snapshot.loading ? "Refreshing..." : "Refresh"
        )
      ),
      snapshot.error &&
        h(
          BSAlert,
          { variant: "warning", className: "stash-tag-curator-alert" },
          "Rules audit unavailable: ",
          String(snapshot.error),
          h("br", null),
          "Run the Rules Audit report task or any mutation task to generate it."
        ),
      !data && snapshot.loading
        ? h(
            "div",
            { className: "stash-tag-curator-loading" },
            h(BSSpinner, { animation: "border", size: "sm" }),
            " Loading..."
          )
        : data
        ? h(
            "div",
            { className: "stash-tag-curator-rulesaudit-body" },
            h(
              "section",
              { className: "stash-tag-curator-section" },
              h("h4", null, "Rules"),
              h(
                "dl",
                { className: "stash-tag-curator-kv" },
                h("dt", null, "Version"),
                h("dd", null, renderValue(data.rules_version)),
                h("dt", null, "Checksum"),
                h(
                  "dd",
                  { className: "stash-tag-curator-mono" },
                  h(
                    "span",
                    { className: "stash-tag-curator-mono-trunc" },
                    data.rules_checksum || "\u2014"
                  )
                ),
                h("dt", null, "Total mappings"),
                h("dd", null, renderValue(data.total_mappings)),
                h("dt", null, "Total canonical tags"),
                h("dd", null, renderValue(data.total_canonical_tags))
              )
            ),
            h(
              "section",
              { className: "stash-tag-curator-section" },
              h("h4", null, "Protected"),
              h(
                "dl",
                { className: "stash-tag-curator-kv" },
                h("dt", null, "Protected prefixes"),
                h(
                  "dd",
                  null,
                  (data.protected_prefixes || []).length
                    ? (data.protected_prefixes || []).map((pfx, i) =>
                        h(
                          BSBadge,
                          {
                            key: "pfx-" + i,
                            variant: "secondary",
                            className: "stash-tag-curator-prefix-badge",
                          },
                          String(pfx)
                        )
                      )
                    : h("span", { className: "stash-tag-curator-muted" }, "(none)")
                ),
                h("dt", null, "Protected tag names"),
                h(
                  "dd",
                  null,
                  h(
                    "span",
                    { className: "stash-tag-curator-muted" },
                    String(data.protected_tag_names_count || 0) + " name(s)"
                  )
                )
              )
            ),
            h(
              "section",
              { className: "stash-tag-curator-section" },
              h("h4", null, "Canonical tags per axis"),
              h(
                "table",
                { className: "stash-tag-curator-axis-table" },
                h(
                  "thead",
                  null,
                  h(
                    "tr",
                    null,
                    h("th", null, "Axis"),
                    h("th", null, "Canonical tags")
                  )
                ),
                h(
                  "tbody",
                  null,
                  Object.keys(data.canonical_tag_counts || {}).map((axis) =>
                    h(
                      "tr",
                      { key: axis },
                      h("td", null, String(axis)),
                      h("td", null, String(data.canonical_tag_counts[axis]))
                    )
                  )
                )
              )
            ),
            h(
              "section",
              { className: "stash-tag-curator-section" },
              h("h4", null, "Mapping dispositions"),
              h(
                "table",
                { className: "stash-tag-curator-disposition-table" },
                h(
                  "thead",
                  null,
                  h(
                    "tr",
                    null,
                    h("th", null, "Disposition"),
                    h("th", null, "Count")
                  )
                ),
                h(
                  "tbody",
                  null,
                  Object.keys(data.mapping_disposition_counts || {}).map((disp) =>
                    h(
                      "tr",
                      { key: disp },
                      h("td", null, String(disp)),
                      h("td", null, String(data.mapping_disposition_counts[disp]))
                    )
                  )
                )
              )
            )
          )
        : null
    );
  }
  // ------------------------------------------------------------------
  // Root component
  // ------------------------------------------------------------------

  function App() {
    const jobState = useJob();
    const dashboard = useDashboard(jobState.job ? jobState.job.status : null);
    const rulesAudit = useAssetSnapshot("rules_audit", false);
    const [opToConfirm, setOpToConfirm] = useState(null);
    const [pendingArgs, setPendingArgs] = useState({});
    const [cleanupProposal, setCleanupProposal] = useState(null);
    const [activeTab, setActiveTab] = useState("dashboard");

    // Whenever a job goes terminal, refresh the dashboard so new totals show.
    useEffect(() => {
      if (jobState.result) {
        dashboard.refresh();
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [jobState.result]);

    return h(
      "main",
      { className: "stash-tag-curator-root" },
      h(
        "header",
        { className: "stash-tag-curator-header" },
        h("h2", null, "Stash Tag Curator"),
        h(
          "p",
          { className: "stash-tag-curator-tagline" },
          "Curation dashboard, operations, and review queue."
        )
      ),
      h(JobPanel, { jobState: jobState }),
      h(
        BSTabs,
        {
          id: "stash-tag-curator-tabs",
          activeKey: activeTab,
          onSelect: (k) => setActiveTab(k || "dashboard"),
          className: "stash-tag-curator-tabs",
        },
        h(
          BSTab,
          { eventKey: "dashboard", title: "Dashboard" },
          h(DashboardPanel, { dashboard: dashboard })
        ),
        h(
          BSTab,
          { eventKey: "operations", title: "Operations" },
          h(OperationsPanel, {
            dashboard: dashboard,
            jobState: jobState,
            opToConfirm: opToConfirm,
            setOpToConfirm: setOpToConfirm,
            pendingArgs: pendingArgs,
            setPendingArgs: setPendingArgs,
            cleanupProposal: cleanupProposal,
            setCleanupProposal: setCleanupProposal,
          })
        ),
        h(
          BSTab,
          { eventKey: "unmapped", title: "Unmapped Tags" },
          h(UnmappedTagsPanel, {
            jobState: jobState,
            autoRefresh: !!(jobState.job && !isTerminalStatus(jobState.job.status)),
            onRulesChanged: dashboard.refresh,
            onRulesAuditRefresh: rulesAudit.refresh,
          })
        ),
        h(
          BSTab,
          { eventKey: "runhistory", title: "Run History" },
          h(RunHistoryPanel, {
            jobState: jobState,
            autoRefresh: !!(jobState.job && !isTerminalStatus(jobState.job.status)),
            onRunChanged: dashboard.refresh,
          })
        ),
        h(
          BSTab,
          { eventKey: "rulesaudit", title: "Rules Audit" },
          h(RulesAuditPanel, { snapshot: rulesAudit })
        )
      )
    );
  }

  // ------------------------------------------------------------------
  // Route registration
  // ------------------------------------------------------------------

  try {
    api.register.route(ROUTE_PATH, App);
  } catch (error) {
    console.error("[stash-tag-curator] route registration failed", error);
  }
})();

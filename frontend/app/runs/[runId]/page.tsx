"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Download, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef } from "react";

import { ApprovalCard } from "@/components/approval-card";
import { RunTopBar } from "@/components/run-topbar";
import { ScreenshotViewer } from "@/components/screenshot-viewer";
import { StepTimeline } from "@/components/step-timeline";
import { Terminal } from "@/components/terminal";
import { api, API_BASE, isTerminal, type Run } from "@/lib/api";
import { formatSeconds, playApprovalChime, subscribeToRun } from "@/lib/utils";
import { useRunStore, useToastStore } from "@/store";

const REFRESH_EVENTS = [
  "step_completed", "step_failed", "run_completed", "run_failed",
  "run_cancelled", "approval_decision", "plan_created", "screenshot",
];

/**
 * The run page.
 *
 * Live state comes from SSE (re-subscribing on reconnect); the run/step/approval
 * records come from React Query, refetched on the events that can change them.
 * That split keeps the log instant without ever trusting the client to be the
 * source of truth for a gated action.
 */
export default function RunPage() {
  const { runId } = useParams<{ runId: string }>();
  const queryClient = useQueryClient();
  const push = useToastStore((s) => s.push);
  const store = useRunStore();

  const runQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId as string),
    refetchInterval: (query) =>
      isTerminal(query.state.data?.status ?? "pending") ? false : 2500,
  });
  const run = runQuery.data;

  useEffect(() => {
    if (!runId) return;
    useRunStore.getState().attach(runId);
    let cancelled = false;
    api
      .getEvents(runId, 0, 400)
      .then((data) => {
        if (!cancelled) useRunStore.getState().seedHistory(data.items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    return subscribeToRun(API_BASE, runId, {
      onOpen: () => useRunStore.getState().setConnected(true),
      onReconnecting: (attempt) => useRunStore.getState().setReconnectAttempt(attempt),
      onDone: () => useRunStore.getState().setConnected(false),
      onEvent: (event) => {
        const state = useRunStore.getState();
        state.pushEvent({ ...event, stepId: null });

        if (event.type === "approval_requested") {
          if (state.soundEnabled) playApprovalChime();
          push({ kind: "warning", title: "TaskPilot needs your approval" });
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            new Notification("TaskPilot needs your approval", {
              body: String(event.message ?? "A risky action is waiting for you."),
            });
          }
        }
        if (REFRESH_EVENTS.includes(event.type)) {
          void queryClient.invalidateQueries({ queryKey: ["run", runId] });
        }
        if (["run_completed", "run_failed", "run_cancelled"].includes(event.type)) {
          useRunStore.getState().setConnected(false);
          void queryClient.invalidateQueries({ queryKey: ["summary", runId] });
        }
      },
    });
  }, [runId, queryClient, push]);

  if (runQuery.isLoading) return <RunSkeleton />;

  if (runQuery.isError || !run) {
    return (
      <div className="card mx-auto max-w-lg p-8 text-center">
        <AlertTriangle className="mx-auto h-6 w-6 text-amber" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">Could not load this run</h1>
        <p className="mt-1 text-sm text-muted">{(runQuery.error as Error)?.message}</p>
        <div className="mt-5 flex justify-center gap-2">
          <button type="button" onClick={() => runQuery.refetch()} className="btn-ghost">
            <RefreshCw className="h-4 w-4" aria-hidden /> Retry
          </button>
          <Link href="/" className="btn-primary">Back home</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <RunTopBar run={run} />
      <h1 className="text-balance text-xl font-semibold tracking-tight" data-testid="run-goal">
        {run.goal}
      </h1>

      {run.pending_approval ? (
        <ApprovalCard
          runId={run.id}
          stepId={pendingStepId(run)}
          approval={run.pending_approval}
          onDecided={() => void queryClient.invalidateQueries({ queryKey: ["run", run.id] })}
        />
      ) : null}

      {isTerminal(run.status) ? <CompletionCard run={run} /> : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
        <div className="space-y-5">
          <section aria-label="Plan and steps">
            <div className="mb-2.5 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Steps</h2>
              <span className="font-mono text-[11px] text-faint">
                {run.steps.filter((s) => s.needs_approval).length} gated
              </span>
            </div>
            <StepTimeline steps={run.steps} />
          </section>
          <ScreenshotViewer frames={store.screenshots} />
        </div>

        <div className="lg:sticky lg:top-20">
          <Terminal
            lines={store.lines}
            connected={store.connected}
            reconnectAttempt={store.reconnectAttempt}
          />
        </div>
      </div>
    </div>
  );
}

/** The API exposes steps by their plan id (s1, s2, …) in the approval URL. */
function pendingStepId(run: Run): string {
  const approval = run.pending_approval;
  if (!approval) return "";
  const step = run.steps.find((s) => s.id === approval.step_id);
  const args = (step?.args ?? {}) as Record<string, unknown>;
  return String(args.__plan_step_id ?? `s${(step?.index ?? 0) + 1}`);
}

/** Completion screen: answer, time saved, downloads, gate integrity. */
function CompletionCard({ run }: { run: Run }) {
  const { data: summary } = useQuery({
    queryKey: ["summary", run.id],
    queryFn: () => api.getSummary(run.id),
    enabled: isTerminal(run.status),
  });

  const ok = run.status === "completed";
  const answer = (run.result?.final_answer as string) ?? summary?.output ?? "";

  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      data-testid="completion-card"
      className={`card overflow-hidden ${ok ? "border-ok/30" : "border-danger/30"}`}
    >
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-4">
        {ok ? (
          <CheckCircle2 className="h-5 w-5 text-ok" aria-hidden />
        ) : (
          <AlertTriangle className="h-5 w-5 text-amber" aria-hidden />
        )}
        <h2 className="text-lg font-semibold">
          {ok ? "Task completed!" : run.status === "cancelled" ? "Run cancelled" : "Run stopped"}
        </h2>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {summary ? (
            <span className="pill border-line2 text-muted" title="Agent time vs the manual baseline">
              {formatSeconds(summary.duration_seconds)}
              {summary.manual_baseline_seconds ? (
                <span className="text-ok">{" · saved "}{formatSeconds(summary.time_saved_seconds ?? 0)}</span>
              ) : null}
            </span>
          ) : null}
          <a href={`${API_BASE}/api/runs/${run.id}/report.md`} className="btn-ghost btn-sm" data-testid="download-md">
            <Download className="h-3.5 w-3.5" aria-hidden /> Markdown
          </a>
          <a href={`${API_BASE}/api/runs/${run.id}/report.pdf`} className="btn-ghost btn-sm" data-testid="download-pdf">
            <Download className="h-3.5 w-3.5" aria-hidden /> PDF
          </a>
          <a href={`${API_BASE}/api/runs/${run.id}/report.json`} className="btn-ghost btn-sm">JSON</a>
          <Link href={`/audit/${run.id}`} className="btn-ghost btn-sm">Audit</Link>
        </div>
      </header>

      <div className="space-y-4 px-5 py-4">
        {answer ? (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{answer}</p>
        ) : null}

        {run.error ? (
          <div className="rounded-xl border border-amber/40 bg-amber/5 p-3">
            <p className="text-xs font-semibold uppercase tracking-wider text-amber">
              {run.error.code}
            </p>
            <p className="mt-1 text-sm text-muted">{run.error.message}</p>
          </div>
        ) : null}

        {summary ? (
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Step accuracy" value={`${Math.round(summary.step_accuracy * 100)}%`} />
            <Stat label="Steps" value={`${summary.steps_succeeded}/${summary.steps_total}`} />
            <Stat
              label="Approvals"
              value={String(summary.approvals_requested)}
              hint={`${summary.approvals_granted} approved, ${summary.approvals_rejected} declined`}
            />
            <Stat
              label="Unapproved risky actions"
              value={String(summary.risky_actions_executed_without_approval)}
              tone={summary.risky_actions_executed_without_approval === 0 ? "ok" : "danger"}
            />
          </dl>
        ) : null}
      </div>
    </motion.section>
  );
}

function Stat({ label, value, hint, tone }: {
  label: string;
  value: string;
  hint?: string;
  tone?: "ok" | "danger";
}) {
  return (
    <div className="rounded-xl border border-line bg-canvas p-3">
      <dt className="text-[10px] uppercase tracking-wider text-faint">{label}</dt>
      <dd
        className={`mt-1 font-mono text-lg font-semibold ${
          tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-ink"
        }`}
      >
        {value}
      </dd>
      {hint ? <p className="mt-0.5 text-[10px] text-faint">{hint}</p> : null}
    </div>
  );
}

function RunSkeleton() {
  return (
    <div className="animate-pulse2 space-y-5" aria-busy="true" aria-label="Loading run">
      <div className="h-6 w-48 rounded-lg bg-line" />
      <div className="h-20 w-full rounded-2xl bg-line" />
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <div key={index} className="h-16 rounded-2xl bg-line" />
          ))}
        </div>
        <div className="h-96 rounded-2xl bg-line" />
      </div>
    </div>
  );
}

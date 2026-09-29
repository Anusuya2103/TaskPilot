"use client";

import { useMutation } from "@tanstack/react-query";
import { Ban, Clock, Loader2, Radio } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { api, isTerminal, type Run } from "@/lib/api";
import { cn, formatDuration } from "@/lib/utils";
import { useRunStore, useToastStore } from "@/store";

const TONE: Record<string, string> = {
  planning: "border-brand/40 bg-brand/10 text-brand",
  pending: "border-line2 text-muted",
  running: "border-brand/40 bg-brand/10 text-brand",
  awaiting_approval: "border-amber/50 bg-amber/10 text-amber",
  awaiting_plan_approval: "border-amber/50 bg-amber/10 text-amber",
  completed: "border-ok/40 bg-ok/10 text-ok",
  failed: "border-danger/50 bg-danger/10 text-danger",
  cancelled: "border-line2 text-faint",
};

/** Top bar: status pill, live elapsed timer, cancel, and a link to the audit log. */
export function RunTopBar({ run }: { run: Run }) {
  const push = useToastStore((s) => s.push);
  const setError = useRunStore((s) => s.setError);
  const finished = isTerminal(run.status);
  const [elapsed, setElapsed] = useState(run.duration_ms ?? 0);

  useEffect(() => {
    if (finished) {
      setElapsed(run.duration_ms ?? 0);
      return;
    }
    const startedAt = run.started_at ? new Date(run.started_at).getTime() : Date.now();
    const tick = () => setElapsed(Date.now() - startedAt);
    tick();
    const timer = setInterval(tick, 500);
    return () => clearInterval(timer);
  }, [finished, run.duration_ms, run.started_at]);

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(run.id),
    onSuccess: (result) => push({ kind: result.accepted ? "warning" : "info", title: result.message }),
    onError: (error) => setError((error as Error).message),
  });

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className={cn("pill", TONE[run.status] ?? "border-line2 text-muted")} data-testid="run-status">
        {!finished ? <Radio className="h-3 w-3 animate-pulse2" aria-hidden /> : null}
        {run.status.replace(/_/g, " ")}
      </span>

      <span className="flex items-center gap-1.5 font-mono text-xs text-muted">
        <Clock className="h-3.5 w-3.5 text-faint" aria-hidden />
        <span data-testid="elapsed">{formatDuration(elapsed)}</span>
        {run.manual_baseline_seconds ? (
          <span className="text-faint">
            of ~{formatDuration(run.manual_baseline_seconds * 1000)} manual
          </span>
        ) : null}
      </span>

      <span className="font-mono text-[11px] text-faint">
        {run.steps_succeeded}/{run.steps_total || run.steps.length} steps
        {run.approvals_count ? ` · ${run.approvals_count} approval${run.approvals_count === 1 ? "" : "s"}` : ""}
      </span>

      <div className="ml-auto flex items-center gap-2">
        <Link
          href={`/audit/${run.id}`}
          className="btn-ghost btn-sm"
          title="Full immutable event log"
        >
          Audit log
        </Link>
        {!finished ? (
          <button
            type="button"
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending}
            data-testid="cancel-run"
            className="btn-danger btn-sm"
          >
            {cancel.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Ban className="h-3.5 w-3.5" aria-hidden />
            )}
            Cancel run
          </button>
        ) : null}
      </div>
    </div>
  );
}

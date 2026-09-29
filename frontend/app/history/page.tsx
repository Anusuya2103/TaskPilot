"use client";

import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Inbox, Search } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { api, type RunListItem, type RunStatus } from "@/lib/api";
import { cn, formatDuration, formatTime, truncate } from "@/lib/utils";

const FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "awaiting_approval", label: "Awaiting approval" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

const TONE: Record<RunStatus, string> = {
  completed: "text-ok border-ok/40 bg-ok/10",
  failed: "text-danger border-danger/40 bg-danger/10",
  cancelled: "text-faint border-line2",
  running: "text-brand border-brand/40 bg-brand/10",
  planning: "text-brand border-brand/40 bg-brand/10",
  awaiting_approval: "text-amber border-amber/50 bg-amber/10",
  awaiting_plan_approval: "text-amber border-amber/50 bg-amber/10",
  pending: "text-muted border-line2",
};

/** Run history: every run, filterable, with duration and approval counts. */
export default function HistoryPage() {
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["runs", status, search],
    queryFn: () => api.listRuns({ status, search, limit: 50 }),
    refetchInterval: 5000,
  });

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">History</h1>
          <p className="mt-1 text-sm text-muted">
            Every run TaskPilot has executed, with what it cost you in approvals.
          </p>
        </div>
        <Link href="/" className="btn-primary btn-sm">New task</Link>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map((filter) => (
          <button
            key={filter.value}
            type="button"
            onClick={() => setStatus(filter.value)}
            aria-pressed={status === filter.value}
            className={cn(
              "chip",
              status === filter.value && "border-amber/60 bg-amber/10 text-amber",
            )}
          >
            {filter.label}
          </button>
        ))}
        <label className="relative ml-auto flex items-center">
          <Search className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-faint" aria-hidden />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search goals…"
            aria-label="Search runs"
            className="input w-56 pl-8"
          />
        </label>
      </div>

      {isLoading ? (
        <div className="space-y-2" aria-busy="true">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="h-16 animate-pulse2 rounded-2xl bg-line" />
          ))}
        </div>
      ) : isError ? (
        <div className="card p-8 text-center">
          <p className="text-sm text-danger">{(error as Error)?.message}</p>
          <p className="mt-1 text-xs text-faint">Is the backend running on port 8000?</p>
        </div>
      ) : !data?.items.length ? (
        <EmptyState searching={Boolean(search)} />
      ) : (
        <ul className="space-y-2" data-testid="run-list">
          {data.items.map((run, index) => (
            <motion.li
              key={run.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, delay: Math.min(index * 0.02, 0.2) }}
            >
              <RunRow run={run} />
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RunRow({ run }: { run: RunListItem }) {
  const saved =
    run.manual_baseline_seconds && run.duration_ms
      ? (run.manual_baseline_seconds - run.duration_ms / 1000)
      : null;
  return (
    <Link
      href={`/runs/${run.id}`}
      className="card card-hover flex flex-wrap items-center gap-3 px-4 py-3"
      data-testid="run-row"
    >
      <span className={cn("pill w-fit text-[10px]", TONE[run.status])}>
        {run.status.replace(/_/g, " ")}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">
          {run.goal || truncate(run.prompt, 70)}
        </span>
        <span className="mt-0.5 block font-mono text-[11px] text-faint">
          {formatTime(run.created_at)} · {formatDuration(run.duration_ms)}
          {saved !== null && saved > 0 ? ` · saved ${formatDuration(saved * 1000)}` : ""}
        </span>
      </span>
      <span className="font-mono text-[11px] text-muted">
        {run.steps_succeeded}/{run.steps_total} steps
      </span>
      {run.approvals_count ? (
        <span className="pill border-amber/40 bg-amber/10 text-[10px] text-amber">
          {run.approvals_count} approval{run.approvals_count === 1 ? "" : "s"}
        </span>
      ) : (
        <span className="pill border-line text-[10px] text-faint">no approvals</span>
      )}
    </Link>
  );
}

function EmptyState({ searching }: { searching: boolean }) {
  return (
    <div className="card flex flex-col items-center gap-3 p-12 text-center">
      <Inbox className="h-7 w-7 text-line2" aria-hidden />
      <p className="text-sm font-medium">
        {searching ? "No runs match that search" : "No runs yet"}
      </p>
      <p className="max-w-xs text-xs text-faint">
        {searching
          ? "Try a different word, or clear the filter."
          : "Give TaskPilot a task and it will show up here with its full audit trail."}
      </p>
      {!searching ? (
        <Link href="/" className="btn-primary btn-sm mt-1">Run your first task</Link>
      ) : null}
    </div>
  );
}

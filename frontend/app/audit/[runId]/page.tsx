"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, FileText, ScrollText } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import { api, API_BASE } from "@/lib/api";
import { cn, formatTime, prettyJson } from "@/lib/utils";

const GROUPS: { value: string; label: string; types?: string[] }[] = [
  { value: "all", label: "All" },
  { value: "plan", label: "Plan", types: ["plan_created", "plan_revised", "replan"] },
  { value: "steps", label: "Steps", types: ["step_started", "step_completed", "step_failed", "step_skipped", "retry"] },
  { value: "tools", label: "Tools", types: ["tool_call", "observation", "screenshot", "verification"] },
  { value: "approvals", label: "Approvals", types: ["approval_requested", "approval_decision"] },
  { value: "policy", label: "Policy", types: ["policy_decision", "security_notice"] },
  { value: "lifecycle", label: "Lifecycle", types: ["run_created", "run_started", "run_completed", "run_failed", "run_cancelled"] },
];

const TONE: Record<string, string> = {
  approval_requested: "text-amber",
  approval_decision: "text-amber",
  policy_decision: "text-amber",
  security_notice: "text-danger",
  step_failed: "text-danger",
  run_failed: "text-danger",
  step_completed: "text-ok",
  run_completed: "text-ok",
  verification: "text-ok",
  step_started: "text-ink",
  tool_call: "text-brand",
  observation: "text-ink",
  plan_created: "text-brand",
  retry: "text-amber",
  replan: "text-amber",
};

/**
 * The audit page: the immutable event log for one run, filterable by type and
 * exportable. This is the "show your working" view — every decision the agent
 * made and every human decision it was given.
 */
export default function AuditPage() {
  const { runId } = useParams<{ runId: string }>();
  const [group, setGroup] = useState("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["audit", runId],
    queryFn: () => api.getEvents(runId as string, 0, 2000),
  });

  const events = useMemo(() => {
    const items = data?.items ?? [];
    const active = GROUPS.find((g) => g.value === group);
    if (!active?.types) return items;
    const allowed = new Set(active.types);
    return items.filter((event) => allowed.has(event.type));
  }, [data, group]);

  const download = (fmt: "md" | "json") => {
    const link = document.createElement("a");
    link.href = `${API_BASE}/api/runs/${runId}/report.${fmt}`;
    link.download = `${runId}.${fmt}`;
    link.click();
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center gap-3">
        <ScrollText className="h-5 w-5 text-faint" aria-hidden />
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Audit trail</h1>
          <p className="font-mono text-[11px] text-faint">{runId}</p>
        </div>
        <div className="ml-auto flex gap-2">
          <button type="button" onClick={() => download("md")} className="btn-ghost btn-sm">
            <FileText className="h-3.5 w-3.5" aria-hidden /> Report
          </button>
          <button type="button" onClick={() => download("json")} className="btn-ghost btn-sm">
            <Download className="h-3.5 w-3.5" aria-hidden /> JSON
          </button>
          <Link href={`/runs/${runId}`} className="btn-primary btn-sm">Back to run</Link>
        </div>
      </header>

      <div className="flex flex-wrap gap-2">
        {GROUPS.map((item) => (
          <button
            key={item.value}
            type="button"
            onClick={() => setGroup(item.value)}
            aria-pressed={group === item.value}
            className={cn("chip", group === item.value && "border-amber/60 bg-amber/10 text-amber")}
          >
            {item.label}
          </button>
        ))}
        <span className="ml-auto self-center font-mono text-[11px] text-faint">
          {events.length} of {data?.items.length ?? 0} events
        </span>
      </div>

      {isLoading ? (
        <div className="space-y-1.5" aria-busy="true">
          {Array.from({ length: 10 }).map((_, index) => (
            <div key={index} className="h-8 animate-pulse2 rounded-lg bg-line" />
          ))}
        </div>
      ) : isError ? (
        <div className="card p-8 text-center text-sm text-danger">{(error as Error)?.message}</div>
      ) : (
        <div className="card overflow-hidden">
          <table className="w-full text-left text-xs" data-testid="audit-table">
            <thead className="border-b border-line text-[10px] uppercase tracking-wider text-faint">
              <tr>
                <th className="px-3 py-2 font-medium">#</th>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Message</th>
                <th className="px-3 py-2 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {events.map((event) => {
                const open = expanded.has(event.id);
                return (
                  <tr key={event.id} className="align-top">
                    <td className="px-3 py-2 font-mono text-faint">{event.id}</td>
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-faint">
                      {formatTime(event.created_at).split(" ").pop()}
                    </td>
                    <td className={cn("px-3 py-2 font-mono", TONE[event.type] ?? "text-muted")}>
                      {event.type}
                    </td>
                    <td className="px-3 py-2 text-ink/90">{event.message || "—"}</td>
                    <td className="px-3 py-2 text-right">
                      {Object.keys(event.payload ?? {}).length > 0 ? (
                        <button
                          type="button"
                          onClick={() =>
                            setExpanded((prev) => {
                              const next = new Set(prev);
                              if (next.has(event.id)) next.delete(event.id);
                              else next.add(event.id);
                              return next;
                            })
                          }
                          className="font-mono text-[10px] text-faint hover:text-amber"
                          aria-expanded={open}
                        >
                          {open ? "hide" : "payload"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {events.map((event) =>
            expanded.has(event.id) ? (
              <pre
                key={`payload-${event.id}`}
                className="max-h-72 overflow-auto border-t border-line bg-canvas p-3 font-mono text-[11px] text-muted"
              >
                {prettyJson(event.payload, 6000)}
              </pre>
            ) : null,
          )}
        </div>
      )}
    </div>
  );
}

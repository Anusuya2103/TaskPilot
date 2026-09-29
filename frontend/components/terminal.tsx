"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownToLine, Search } from "lucide-react";

import { cn, formatClock } from "@/lib/utils";
import type { LogLine } from "@/store";

/** Event type -> terminal colour. Amber marks the moments that need a human. */
const TONE: Record<string, string> = {
  run_created: "text-muted",
  run_started: "text-brand",
  plan_created: "text-brand",
  step_started: "text-ink",
  tool_call: "text-brand",
  observation: "text-ink",
  verification: "text-ok",
  screenshot: "text-faint",
  approval_requested: "text-amber",
  approval_decision: "text-amber",
  step_completed: "text-ok",
  step_failed: "text-danger",
  step_skipped: "text-faint",
  retry: "text-amber",
  replan: "text-amber",
  policy_decision: "text-amber",
  security_notice: "text-danger",
  run_completed: "text-ok",
  run_failed: "text-danger",
  run_cancelled: "text-faint",
  log: "text-muted",
};

const TYPES = [
  "all", "step", "tool", "approval", "policy", "security", "terminal",
] as const;

/**
 * The "TaskPilot.exe" terminal.
 *
 * Streams the SSE feed as a scrolling log with a filter. It is deliberately
 * dense and monospaced: during a live run this is the panel people actually
 * watch, so it has to feel like the machine is talking.
 */
export function Terminal({ lines, connected, reconnectAttempt }: {
  lines: LogLine[];
  connected: boolean;
  reconnectAttempt: number;
}) {
  const [filter, setFilter] = useState<(typeof TYPES)[number]>("all");
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const endRef = useRef<HTMLDivElement | null>(null);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return lines.filter((line) => {
      if (filter !== "all" && !matchType(line.type, filter)) return false;
      if (!needle) return true;
      return (
        line.message.toLowerCase().includes(needle) ||
        line.type.toLowerCase().includes(needle)
      );
    });
  }, [lines, filter, query]);

  useEffect(() => {
    if (follow) endRef.current?.scrollIntoView({ block: "end" });
  }, [visible.length, follow]);

  return (
    <section className="card flex min-h-0 flex-col overflow-hidden" aria-label="Live run log">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <span className="flex items-center gap-1.5 font-mono text-[11px] text-muted">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              connected ? "bg-ok animate-pulse2" : "bg-amber",
            )}
            aria-hidden
          />
          TaskPilot.exe
        </span>
        {!connected ? (
          <span className="font-mono text-[10px] text-amber" data-testid="sse-reconnecting">
            {reconnectAttempt > 0 ? `reconnecting (${reconnectAttempt})…` : "reconnecting…"}
          </span>
        ) : null}

        <div className="ml-auto flex items-center gap-1">
          {TYPES.map((type) => (
            <button
              key={type}
              type="button"
              onClick={() => setFilter(type)}
              aria-pressed={filter === type}
              className={cn(
                "rounded-md px-1.5 py-1 font-mono text-[10px] transition-colors",
                filter === type
                  ? "bg-amber/15 text-amber"
                  : "text-faint hover:text-ink",
              )}
            >
              {type}
            </button>
          ))}
        </div>

        <label className="relative flex items-center">
          <Search className="pointer-events-none absolute left-2 h-3 w-3 text-faint" aria-hidden />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="grep"
            aria-label="Filter log lines"
            className="w-24 rounded-md border border-line bg-canvas py-1 pl-7 pr-2 font-mono text-[10px] text-ink placeholder:text-faint focus:border-amber/50 focus:outline-none"
          />
        </label>

        <button
          type="button"
          onClick={() => setFollow((v) => !v)}
          aria-pressed={follow}
          title={follow ? "Following the tail" : "Scroll locked"}
          className={cn(
            "flex items-center gap-1 rounded-md px-1.5 py-1 font-mono text-[10px] transition-colors",
            follow ? "text-ok" : "text-faint hover:text-ink",
          )}
        >
          <ArrowDownToLine className="h-3 w-3" aria-hidden />
          {follow ? "live" : "paused"}
        </button>
      </header>

      <div
        className="min-h-[14rem] flex-1 overflow-y-auto bg-canvas px-3 py-2 font-mono text-[11px] leading-relaxed"
        role="log"
        aria-live="polite"
        data-testid="terminal-log"
        onScroll={(event) => {
          const el = event.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          if (atBottom !== follow) setFollow(atBottom);
        }}
      >
        {visible.length === 0 ? (
          <p className="py-6 text-center text-faint">
            {lines.length === 0 ? "Waiting for the first event…" : "No lines match this filter."}
          </p>
        ) : (
          visible.map((line) => (
            <div key={`${line.id}-${line.type}`} className="flex gap-2 hover:bg-line/30">
              <span className="shrink-0 text-faint">{formatClock(line.at)}</span>
              <span className={cn("w-40 shrink-0 truncate", TONE[line.type] ?? "text-muted")}>
                {line.type}
              </span>
              <span className="min-w-0 flex-1 break-words text-ink/90">{line.message}</span>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </section>
  );
}

function matchType(eventType: string, filter: string): boolean {
  switch (filter) {
    case "step":
      return eventType.startsWith("step_") || eventType === "retry" || eventType === "replan";
    case "tool":
      return eventType === "tool_call" || eventType === "observation" || eventType === "screenshot";
    case "approval":
      return eventType.startsWith("approval_") || eventType.startsWith("plan_");
    case "policy":
      return eventType === "policy_decision";
    case "security":
      return eventType === "security_notice";
    case "terminal":
      return eventType.startsWith("run_");
    default:
      return true;
  }
}

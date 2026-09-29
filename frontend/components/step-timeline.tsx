"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  Ban, CheckCircle2, ChevronRight, CircleDashed, CircleX,
  Loader2, PauseCircle, ShieldAlert,
} from "lucide-react";
import { useState } from "react";

import type { Step } from "@/lib/api";
import { cn, formatClock, formatDuration, prettyJson, truncate } from "@/lib/utils";
import { stepTone } from "@/store";

const ICONS: Record<string, typeof CheckCircle2> = {
  done: CheckCircle2, failed: CircleX, skipped: Ban,
  awaiting_approval: PauseCircle, blocked: PauseCircle,
  running: Loader2, pending: CircleDashed,
};

/**
 * The animated step timeline. Each row expands to show the tool, the exact
 * arguments, the observation and how long it took — detail on demand, without
 * cluttering the happy path.
 */
export function StepTimeline({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});

  if (steps.length === 0) {
    return (
      <div className="card p-6 text-center text-sm text-faint" data-testid="timeline-empty">
        The plan will appear here once TaskPilot has finished thinking.
      </div>
    );
  }

  return (
    <ol className="relative space-y-2" data-testid="step-timeline">
      <span className="absolute bottom-4 left-[15px] top-4 w-px bg-line" aria-hidden />
      <AnimatePresence initial={false}>
        {steps.map((step) => {
          const Icon = ICONS[step.status] ?? CircleDashed;
          const isOpen = open[step.id] ?? false;
          return (
            <motion.li
              key={step.id}
              layout
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.22, ease: "easeOut" }}
              className="relative pl-10"
            >
              <span
                className={cn(
                  "absolute left-0 top-3 grid h-8 w-8 place-items-center rounded-xl border bg-raised",
                  stepTone(step.status),
                )}
                aria-hidden
              >
                <Icon className={cn("h-4 w-4", step.status === "running" && "animate-spin")} />
              </span>

              <div
                className={cn(
                  "card overflow-hidden transition-colors",
                  step.status === "awaiting_approval" && "border-amber/50",
                )}
              >
                <button
                  type="button"
                  onClick={() => setOpen((p) => ({ ...p, [step.id]: !p[step.id] }))}
                  aria-expanded={isOpen}
                  data-testid={`step-row-${step.index}`}
                  className="flex w-full items-center gap-3 px-3.5 py-3 text-left transition-colors hover:bg-line/40"
                >
                  <ChevronRight
                    className={cn("h-3.5 w-3.5 shrink-0 text-faint transition-transform", isOpen && "rotate-90")}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{step.goal}</span>
                      {step.needs_approval ? (
                        <span
                          className="pill shrink-0 border-amber/40 bg-amber/10 text-[10px] text-amber"
                          title="This step needs a human approval before it can run"
                        >
                          <ShieldAlert className="h-2.5 w-2.5" aria-hidden />
                          approval
                        </span>
                      ) : null}
                    </span>
                    <span className="mt-0.5 flex items-center gap-2 font-mono text-[11px] text-faint">
                      <span>#{step.index + 1}</span>
                      <span>{step.tool}</span>
                      {step.latency_ms ? <span>· {formatDuration(step.latency_ms)}</span> : null}
                      {step.attempts > 1 ? (
                        <span className="text-amber">· {step.attempts} attempts</span>
                      ) : null}
                    </span>
                  </span>
                  <span
                    className={cn(
                      "pill shrink-0 text-[10px]",
                      step.effective_risk === "high"
                        ? "border-amber/40 bg-amber/10 text-amber"
                        : step.effective_risk === "medium"
                          ? "border-line2 text-muted"
                          : "border-line text-faint",
                    )}
                  >
                    {step.effective_risk}
                  </span>
                </button>

                <AnimatePresence initial={false}>
                  {isOpen ? (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      className="overflow-hidden"
                    >
                      <StepDetail step={step} />
                    </motion.div>
                  ) : null}
                </AnimatePresence>
              </div>
            </motion.li>
          );
        })}
      </AnimatePresence>
    </ol>
  );
}

/** Expanded body of a timeline row. */
function StepDetail({ step }: { step: Step }) {
  return (
    <div className="space-y-3 border-t border-line px-4 py-3 text-xs">
      {step.expected_outcome ? (
        <p className="text-muted">
          <span className="text-faint">Expected:</span> {step.expected_outcome}
        </p>
      ) : null}

      <div>
        <p className="label">Arguments</p>
        <pre className="max-h-40 overflow-auto rounded-lg border border-line bg-canvas p-2.5 font-mono text-[11px] text-muted">
          {prettyJson(step.args, 1200)}
        </pre>
      </div>

      {step.observation ? (
        <div>
          <p className="label">Observation</p>
          <p className="rounded-lg border border-line bg-canvas p-2.5 text-[11px] text-muted">
            {truncate(String(step.observation.summary ?? JSON.stringify(step.observation)), 600)}
          </p>
        </div>
      ) : null}

      {step.verification ? (
        <p className="flex items-start gap-2 text-[11px]">
          {step.verification.success ? (
            <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-ok" aria-hidden />
          ) : (
            <CircleX className="mt-0.5 h-3 w-3 shrink-0 text-danger" aria-hidden />
          )}
          <span className="text-muted">
            {step.verification.reason}
            <span className="ml-1 text-faint">
              (confidence {Math.round((step.verification.confidence ?? 0) * 100)}%)
            </span>
          </span>
        </p>
      ) : null}

      {step.error ? (
        <p className="rounded-lg border border-danger/40 bg-danger/10 p-2.5 text-[11px] text-danger">
          {step.error.message}
        </p>
      ) : null}

      {step.needs_approval && step.approved_at ? (
        <p className="flex items-center gap-2 text-[11px] text-ok">
          <CheckCircle2 className="h-3 w-3" aria-hidden />
          Approved by a human at {formatClock(step.approved_at)}
        </p>
      ) : null}
    </div>
  );
}

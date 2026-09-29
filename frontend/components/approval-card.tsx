"use client";

import { useMutation } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Check, Loader2, Pencil, X } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type Approval } from "@/lib/api";
import { cn, formatClock, mediaUrl, parseApiDate, prettyJson } from "@/lib/utils";
import { useToastStore } from "@/store";

/**
 * The approval card.
 *
 * It shows the human *what is about to happen*: the action, the target URL, the
 * exact arguments, the screenshot of the live page, and the policy reasons that
 * classified it as risky. Arguments are editable because a common case is
 * "yes, but the 1m one" — and the edit is what the executor then runs.
 */
export function ApprovalCard({
  runId,
  approval,
  stepId,
  onDecided,
}: {
  runId: string;
  approval: Approval;
  stepId: string;
  onDecided?: () => void;
}) {
  const push = useToastStore((s) => s.push);
  const [editing, setEditing] = useState(false);
  const [argsText, setArgsText] = useState(() => JSON.stringify(approval.original_args, null, 2));
  const [argsError, setArgsError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<number | null>(approval.seconds_remaining);

  // Countdown to the auto-reject, so a pause never silently expires on screen.
  useEffect(() => {
    if (!approval.expires_at) return;
    const tick = () => {
      // parseApiDate: the API sends naive UTC, which new Date() would read
      // as local time and make a fresh approval look expired.
      const ms = (parseApiDate(approval.expires_at)?.getTime() ?? 0) - Date.now();
      setRemaining(Math.max(0, Math.round(ms / 1000)));
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [approval.expires_at]);

  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject" | "edit_args") => {
      if (decision === "edit_args") {
        let parsed: Record<string, unknown>;
        try {
          parsed = JSON.parse(argsText) as Record<string, unknown>;
        } catch {
          throw new Error("Arguments must be valid JSON.");
        }
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("Arguments must be a JSON object.");
        }
        setArgsError(null);
        return api.decideApproval(runId, stepId, {
          decision: "edit_args",
          edited_args: parsed,
        });
      }
      return api.decideApproval(runId, stepId, { decision });
    },
    onSuccess: (_data, decision) => {
      push({
        kind: decision === "reject" ? "warning" : "success",
        title:
          decision === "reject"
            ? "Declined — the agent will stop"
            : decision === "edit_args"
              ? "Approved with your edits"
              : "Approved — the agent continues",
      });
      onDecided?.();
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      setArgsError(message);
      push({ kind: "error", title: "Could not record your decision", body: message });
    },
  });

  const preview = approval.preview;
  const screenshot = mediaUrl(approval.screenshot_url ?? preview?.screenshot ?? null);
  const expired = remaining !== null && remaining <= 0;

  return (
    <AnimatePresence>
      <motion.section
        key={approval.id}
        data-testid="approval-card"
        initial={{ opacity: 0, y: 14, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: -8, scale: 0.97 }}
        transition={{ duration: 0.25, ease: "easeOut" }}
        role="alertdialog"
        aria-labelledby="approval-title"
        aria-describedby="approval-desc"
        className="overflow-hidden rounded-2xl border-2 border-amber/60 bg-raised shadow-amber"
      >
        <header className="flex items-start gap-3 border-b border-amber/25 bg-amber/5 px-5 py-4">
          <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-amber text-canvas">
            <AlertTriangle className="h-4 w-4" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold uppercase tracking-wider text-amber">
              Approval required
            </p>
            <h2 id="approval-title" className="mt-0.5 truncate text-base font-semibold">
              {preview?.action ?? approval.reason ?? "Proceed?"}
            </h2>
            <p id="approval-desc" className="mt-1 text-sm text-muted">
              {preview?.consequence ?? "This action cannot be undone by TaskPilot."}
            </p>
          </div>
          {remaining !== null ? (
            <span
              data-testid="approval-countdown"
              className={cn(
                "pill shrink-0 tabular-nums",
                remaining <= 30
                  ? "border-danger/50 bg-danger/10 text-danger"
                  : "border-amber/40 bg-amber/10 text-amber",
              )}
              title="Auto-rejects when it reaches zero"
            >
              {expired
                ? "expired"
                : `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`}
            </span>
          ) : null}
        </header>

        <div className="grid gap-4 p-5 lg:grid-cols-[1fr_18rem]">
          <div className="min-w-0 space-y-3">
            <dl className="grid grid-cols-[6.5rem_1fr] gap-x-3 gap-y-1.5 text-sm">
              <dt className="text-faint">Tool</dt>
              <dd className="font-mono text-xs text-ink">{preview?.tool}</dd>
              <dt className="text-faint">Target</dt>
              <dd className="truncate font-mono text-xs text-ink" title={preview?.target}>
                {preview?.target || "—"}
              </dd>
              {preview?.current_url ? (
                <>
                  <dt className="text-faint">Current page</dt>
                  <dd
                    className="truncate font-mono text-xs text-muted"
                    title={preview.current_url}
                  >
                    {preview.current_url}
                  </dd>
                </>
              ) : null}
              <dt className="text-faint">Risk</dt>
              <dd>
                <span className="pill border-amber/40 bg-amber/10 text-amber">
                  {approval.risk}
                </span>
              </dd>
              <dt className="text-faint">Requested</dt>
              <dd className="text-xs text-muted">{formatClock(approval.requested_at)}</dd>
            </dl>

            {approval.risk_reasons?.length ? (
              <div>
                <p className="label">Why TaskPilot paused</p>
                <ul className="space-y-1">
                  {approval.risk_reasons.map((reason) => (
                    <li key={reason.rule} className="flex gap-2 text-xs text-muted">
                      <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-amber" aria-hidden />
                      <span>{reason.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <p className="label mb-0">Arguments the agent will use</p>
                {!editing ? (
                  <button
                    type="button"
                    onClick={() => setEditing(true)}
                    data-testid="edit-args"
                    className="flex items-center gap-1 text-xs text-muted transition-colors hover:text-amber"
                  >
                    <Pencil className="h-3 w-3" aria-hidden />
                    Edit
                  </button>
                ) : null}
              </div>
              {editing ? (
                <div className="space-y-2">
                  <textarea
                    value={argsText}
                    onChange={(event) => setArgsText(event.target.value)}
                    rows={6}
                    spellCheck={false}
                    data-testid="args-editor"
                    aria-label="Tool arguments as JSON"
                    className="input font-mono text-xs"
                  />
                  {argsError ? (
                    <p className="text-xs text-danger" role="alert">
                      {argsError}
                    </p>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => setEditing(false)}
                    className="text-xs text-muted hover:text-ink"
                  >
                    Cancel editing
                  </button>
                </div>
              ) : (
                <pre
                  data-testid="args-preview"
                  className="max-h-40 overflow-auto rounded-xl border border-line bg-canvas p-3 font-mono text-xs text-muted"
                >
                  {prettyJson(approval.original_args, 1200)}
                </pre>
              )}
            </div>
          </div>

          {screenshot ? (
            <figure className="overflow-hidden rounded-xl border border-line bg-canvas">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={screenshot}
                alt={`Screenshot of ${preview?.page_title || "the page being acted on"}`}
                className="h-full w-full object-cover object-top"
              />
              <figcaption className="border-t border-line px-2 py-1 font-mono text-[10px] text-faint">
                live page at request time
              </figcaption>
            </figure>
          ) : null}
        </div>

        <footer className="flex flex-wrap items-center gap-2 border-t border-line px-5 py-4">
          <button
            type="button"
            onClick={() => decide.mutate("reject")}
            disabled={decide.isPending || expired}
            data-testid="approval-reject"
            className="btn-danger"
          >
            <X className="h-4 w-4" aria-hidden />
            Cancel
          </button>
          <button
            type="button"
            onClick={() => decide.mutate("approve")}
            disabled={decide.isPending || expired}
            data-testid="approval-approve"
            className="btn-primary"
          >
            {decide.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Check className="h-4 w-4" aria-hidden />
            )}
            Approve &amp; continue
          </button>
          {editing ? (
            <button
              type="button"
              onClick={() => decide.mutate("edit_args")}
              disabled={decide.isPending || expired}
              data-testid="approval-edit-submit"
              className="btn-ghost"
            >
              Approve with these args
            </button>
          ) : null}
          <p className="ml-auto text-xs text-faint">
            Auto-rejects on timeout
          </p>
        </footer>
      </motion.section>
    </AnimatePresence>
  );
}

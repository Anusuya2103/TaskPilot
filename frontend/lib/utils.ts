/** Small shared utilities: class merging, formatting, and a tiny SSE client. */

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export { mediaUrl } from "@/lib/api";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function formatSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "--:--:--";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString(undefined, { hour12: false });
}

export function prettyJson(value: unknown, max = 4000): string {
  try {
    const text = JSON.stringify(value, null, 2) ?? "";
    return text.length > max ? `${text.slice(0, max)}\n…` : text;
  } catch {
    return String(value).slice(0, max);
  }
}

export function truncate(text: string, max = 80): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/**
 * Resumable SSE subscription.
 *
 * The backend's `Last-Event-ID` support means a dropped connection is a
 * non-event: we reconnect with the last id we saw and the server replays
 * exactly what we missed. `EventSource` does this for us once `id:` is present
 * on the frames, which it is.
 */
export interface SseHandlers {
  onEvent: (event: { id: number; type: string; message: string; payload: Record<string, unknown> }) => void;
  onOpen?: () => void;
  onReconnecting?: (attempt: number) => void;
  onError?: (error: Event | Error) => void;
  onDone?: () => void;
}

export function subscribeToRun(baseUrl: string, runId: string, handlers: SseHandlers): () => void {
  if (typeof window === "undefined" || typeof EventSource === "undefined") return () => {};

  let source: EventSource | null = null;
  let attempt = 0;
  let closed = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const connect = () => {
    if (closed) return;
    const url = `${baseUrl}/api/runs/${runId}/events`;
    source = new EventSource(url);

    source.addEventListener("open", () => {
      attempt = 0;
      handlers.onOpen?.();
    });

    source.onmessage = (raw) => {
      try {
        const data = JSON.parse(raw.data) as {
          id: number;
          type: string;
          message: string;
          payload: Record<string, unknown>;
        };
        handlers.onEvent(data);
      } catch {
        /* ignore malformed frames rather than tearing down the stream */
      }
    };

    // Named events (our event types) are delivered alongside onmessage.
    const named = [
      "run_created", "run_started", "plan_created", "step_started", "tool_call",
      "observation", "screenshot", "verification", "approval_requested",
      "approval_decision", "step_completed", "step_failed", "step_skipped",
      "retry", "replan", "policy_decision", "security_notice", "run_completed",
      "run_failed", "run_cancelled", "log",
    ];
    for (const type of named) {
      source.addEventListener(type, (raw) => {
        try {
          const data = JSON.parse((raw as MessageEvent).data) as {
            id: number;
            type: string;
            message: string;
            payload: Record<string, unknown>;
          };
          handlers.onEvent({ ...data, type: data.type ?? type });
        } catch {
          /* ignore */
        }
      });
    }

    source.addEventListener("done", () => {
      handlers.onDone?.();
      cleanup();
    });

    source.onerror = (event) => {
      if (closed) return;
      handlers.onError?.(event);
      cleanup();
      attempt += 1;
      handlers.onReconnecting?.(attempt);
      // EventSource reconnects on its own, but only if we leave it alone; we
      // build a fresh one so the Last-Event-ID cursor is re-sent explicitly.
      retryTimer = setTimeout(connect, Math.min(1000 * 2 ** attempt, 8000));
    };
  };

  const cleanup = () => {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (source) {
      source.close();
      source = null;
    }
  };

  connect();
  return () => {
    closed = true;
    cleanup();
  };
}

/** Play a short chime when an approval needs attention (no-op if blocked). */
export function playApprovalChime(): void {
  if (typeof window === "undefined") return;
  try {
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    const notes = [660, 880];
    notes.forEach((frequency, index) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = frequency;
      gain.gain.value = 0.0001;
      osc.connect(gain);
      gain.connect(ctx.destination);
      const start = ctx.currentTime + index * 0.18;
      gain.gain.exponentialRampToValueAtTime(0.15, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.28);
      osc.start(start);
      osc.stop(start + 0.3);
    });
    setTimeout(() => void ctx.close?.(), 900);
  } catch {
    /* autoplay policy or no audio device — the visual cue still fires */
  }
}

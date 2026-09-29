"use client";

import { create } from "zustand";

import type { RunEvent, RunStatus, StepStatus } from "@/lib/api";

/**
 * Ephemeral client state: the live event feed, the current run's status and the
 * pending approval. Everything durable is fetched through React Query, so a
 * reload always reconstructs truth from the server.
 */
export interface LogLine {
  id: number;
  type: string;
  message: string;
  stepId: string | null;
  at: string;
  payload: Record<string, unknown>;
}

export interface ScreenshotFrame {
  src: string;
  url: string;
  at: string;
  label: string;
}

interface RunState {
  runId: string | null;
  status: RunStatus | null;
  connected: boolean;
  reconnectAttempt: number;
  lines: LogLine[];
  screenshots: ScreenshotFrame[];
  lastEventId: number;
  lastError: string | null;
  // Transient UI flags
  planExpanded: boolean;
  soundEnabled: boolean;

  attach: (runId: string) => void;
  detach: () => void;
  setStatus: (status: RunStatus) => void;
  setConnected: (connected: boolean) => void;
  setReconnectAttempt: (attempt: number) => void;
  pushEvent: (event: Omit<LogLine, "at"> & { at?: string }) => void;
  setScreenshots: (frames: ScreenshotFrame[]) => void;
  seedHistory: (events: RunEvent[]) => void;
  setError: (message: string | null) => void;
  setPlanExpanded: (expanded: boolean) => void;
  setSoundEnabled: (enabled: boolean) => void;
}

const MAX_LINES = 600;
const MAX_FRAMES = 60;

export const useRunStore = create<RunState>((set) => ({
  runId: null,
  status: null,
  connected: false,
  reconnectAttempt: 0,
  lines: [],
  screenshots: [],
  lastEventId: 0,
  lastError: null,
  planExpanded: true,
  soundEnabled: true,

  attach: (runId) =>
    set({
      runId,
      lines: [],
      screenshots: [],
      lastEventId: 0,
      lastError: null,
      reconnectAttempt: 0,
    }),
  detach: () => set({ runId: null, connected: false, lines: [], screenshots: [] }),
  setStatus: (status) => set({ status }),
  setConnected: (connected) => set({ connected }),
  setReconnectAttempt: (reconnectAttempt) => set({ reconnectAttempt }),

  pushEvent: (event) =>
    set((state) => {
      // Guard against duplicate delivery at the replay/live boundary.
      if (event.id && event.id <= state.lastEventId) return state;
      const line: LogLine = {
        ...event,
        at: event.at ?? new Date().toISOString(),
        id: event.id || Date.now(),
      };
      const screenshots = [...state.screenshots];
      const src = typeof event.payload?.src === "string" ? event.payload.src : null;
      if (event.type === "screenshot" && src) {
        screenshots.push({
          src,
          url: String(event.payload?.url ?? ""),
          at: line.at,
          label: String(event.payload?.label ?? ""),
        });
        if (screenshots.length > MAX_FRAMES) screenshots.splice(0, screenshots.length - MAX_FRAMES);
      }
      const lines = [...state.lines, line];
      if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
      return {
        lines,
        screenshots,
        lastEventId: Math.max(state.lastEventId, event.id ?? 0),
      };
    }),

  setScreenshots: (frames) => set({ screenshots: frames.slice(-MAX_FRAMES) }),

  seedHistory: (events) =>
    set((state) => {
      const lines: LogLine[] = events.map((event) => ({
        id: event.id,
        type: event.type,
        message: event.message,
        stepId: event.step_id,
        at: event.created_at,
        payload: event.payload,
      }));
      return { lines, lastEventId: events.at(-1)?.id ?? state.lastEventId };
    }),

  setError: (lastError) => set({ lastError }),
  setPlanExpanded: (planExpanded) => set({ planExpanded }),
  setSoundEnabled: (soundEnabled) => set({ soundEnabled }),
}));

/** Toast store — small on purpose; toasts are the only global feedback channel. */
export type ToastKind = "info" | "success" | "warning" | "error";

export interface Toast {
  id: string;
  kind: ToastKind;
  title: string;
  body?: string;
}

interface ToastState {
  toasts: Toast[];
  push: (toast: { kind: ToastKind; title: string; body?: string }) => void;
  dismiss: (id: string) => void;
}

export const useToastStore = create<ToastState>((set, get) => ({
  toasts: [],
  push: (toast) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    set({ toasts: [...get().toasts, { id, ...toast }].slice(-4) });
    setTimeout(() => get().dismiss(id), toast.kind === "error" ? 8000 : 4500);
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

/** Map a step status to the timeline dot colour token. */
export function stepTone(status: StepStatus): string {
  switch (status) {
    case "done":
      return "text-ok border-ok/40 bg-ok/10";
    case "failed":
      return "text-danger border-danger/40 bg-danger/10";
    case "skipped":
      return "text-faint border-line2 bg-surface";
    case "awaiting_approval":
    case "blocked":
      return "text-amber border-amber/50 bg-amber/10";
    case "running":
      return "text-brand border-brand/40 bg-brand/10 animate-pulse2";
    default:
      return "text-faint border-line2 bg-surface";
  }
}

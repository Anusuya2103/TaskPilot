/**
 * Typed API client — shapes mirror `backend/app/core/schemas.py`.
 *
 * Hand-written rather than generated so it is readable, tree-shakeable and
 * reviewable in one sitting. This file is the contract the UI codes against.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

/** SSE lives on the same origin as the API unless explicitly overridden. */
export const SSE_BASE =
  process.env.NEXT_PUBLIC_SSE_BASE_URL?.replace(/\/$/, "") ?? API_BASE;

export type RiskLevel = "low" | "medium" | "high";

export type RunStatus =
  | "pending"
  | "planning"
  | "awaiting_plan_approval"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | "blocked"
  | "awaiting_approval";

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "edited"
  | "rejected"
  | "timed_out"
  | "cancelled";

export interface RiskReason {
  rule: string;
  detail: string;
  risk: RiskLevel;
}

export interface Step {
  id: string;
  run_id: string;
  index: number;
  goal: string;
  tool: string;
  args: Record<string, unknown>;
  expected_outcome: string;
  risk: RiskLevel;
  declared_risk: RiskLevel;
  effective_risk: RiskLevel;
  needs_approval: boolean;
  risk_reasons: RiskReason[];
  status: StepStatus;
  attempts: number;
  observation: Record<string, unknown> | null;
  extracted: Record<string, unknown> | null;
  verification: { success: boolean; reason: string; confidence: number } | null;
  error: { code?: string; message?: string } | null;
  started_at: string | null;
  finished_at: string | null;
  latency_ms: number | null;
  approved_at: string | null;
}

export interface ApprovalPreview {
  action: string;
  tool: string;
  tool_description: string;
  args: Record<string, unknown>;
  args_preview: string;
  risk: RiskLevel;
  declared_risk: RiskLevel;
  rule_risk: RiskLevel;
  reasons: RiskReason[];
  explanation: string;
  target: string;
  current_url: string;
  page_title: string;
  screenshot: string | null;
  reversible: boolean;
  consequence: string;
}

export interface Approval {
  id: string;
  run_id: string;
  step_id: string;
  status: ApprovalStatus;
  reason: string;
  risk: RiskLevel;
  risk_reasons: RiskReason[];
  preview: ApprovalPreview;
  original_args: Record<string, unknown>;
  edited_args: Record<string, unknown> | null;
  screenshot_url: string | null;
  requested_at: string;
  expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  seconds_remaining: number | null;
}

export interface Run {
  id: string;
  task_id: string;
  status: RunStatus;
  goal: string;
  plan: Record<string, unknown> | null;
  plan_revision: number;
  replan_count: number;
  provider: string;
  model: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  manual_baseline_seconds: number | null;
  steps_total: number;
  steps_succeeded: number;
  approvals_count: number;
  result: Record<string, unknown> | null;
  error: { code?: string; message?: string } | null;
  cancel_requested: boolean;
  steps: Step[];
  pending_approval: Approval | null;
  prompt: string;
}

export interface RunListItem {
  id: string;
  task_id: string;
  status: RunStatus;
  goal: string;
  prompt: string;
  created_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  steps_total: number;
  steps_succeeded: number;
  approvals_count: number;
  error: { code?: string; message?: string } | null;
  manual_baseline_seconds: number | null;
}

export interface RunEvent {
  id: number;
  run_id: string;
  step_id: string | null;
  type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  goal: string;
  output: string;
  steps_total: number;
  steps_succeeded: number;
  steps_failed: number;
  steps_skipped: number;
  step_accuracy: number;
  steps_requiring_approval: number;
  approvals_requested: number;
  approvals_granted: number;
  approvals_rejected: number;
  approvals_timed_out: number;
  risky_actions_executed_without_approval: number;
  duration_seconds: number;
  manual_baseline_seconds: number | null;
  time_saved_seconds: number | null;
  extracted: Record<string, unknown>;
  screenshots: string[];
  replans: number;
}

export interface DemoTask {
  id: string;
  title: string;
  prompt: string;
  needs_approval: boolean;
  tools: string[];
  estimated_manual_minutes: number;
  category: string;
  description: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  source: "builtin" | "mcp";
  risk_level: RiskLevel;
  category: string;
  available: boolean;
  server: string | null;
  args_schema: Record<string, unknown>;
  examples: Record<string, unknown>[];
}

export interface PublicConfig {
  version: string;
  env: string;
  llm_provider: string;
  llm_model: string;
  demo_mode: boolean;
  approval_timeout_seconds: number;
  auto_approve_low_risk: boolean;
  high_risk_always_requires_approval: boolean;
  planner_preview: boolean;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
    readonly retryable: boolean,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new ApiError(
      "Could not reach the TaskPilot API. Is the backend running?",
      "network_error",
      0,
      true,
      { cause: String(cause) },
    );
  }

  if (!response.ok) {
    let code = `http_${response.status}`;
    let message = response.statusText || "Request failed";
    let retryable = response.status >= 500;
    let details: Record<string, unknown> = {};
    try {
      const body = await response.json();
      const error = body?.error ?? body?.detail ?? body;
      if (error && typeof error === "object") {
        code = String(error.code ?? code);
        message = String(error.message ?? message);
        retryable = Boolean(error.retryable ?? retryable);
        details = (error.details as Record<string, unknown>) ?? {};
      } else if (typeof error === "string") {
        message = error;
      }
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new ApiError(message, code, response.status, retryable, details);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  config: () => request<PublicConfig>("/api/config"),
  health: () => request<{ status: string; version: string }>("/health"),

  createTask: (body: { prompt: string; review_plan?: boolean; auto_start?: boolean }) =>
    request<Run>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ auto_start: true, ...body }),
    }),

  getRun: (id: string) => request<Run>(`/api/runs/${id}`),
  listRuns: (params: { status?: string; search?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.status && params.status !== "all") query.set("status", params.status);
    if (params.search) query.set("search", params.search);
    query.set("limit", String(params.limit ?? 20));
    return request<{ items: RunListItem[]; total: number }>(`/api/runs?${query}`);
  },
  cancelRun: (id: string) =>
    request<{ accepted: boolean; message: string; status: RunStatus }>(
      `/api/runs/${id}/cancel`,
      { method: "POST" },
    ),
  approvePlan: (id: string) => request<Run>(`/api/runs/${id}/plan/approve`, { method: "POST" }),
  rejectPlan: (id: string) => request<Run>(`/api/runs/${id}/plan/reject`, { method: "POST" }),

  getSummary: (id: string) => request<RunSummary>(`/api/runs/${id}/summary`),
  getReport: (id: string) =>
    request<{
      markdown: string;
      json_report: Record<string, unknown>;
      download_urls: Record<string, string>;
    }>(`/api/runs/${id}/report`),
  getEvents: (id: string, afterId = 0, limit = 500) =>
    request<{ items: RunEvent[]; count: number }>(
      `/api/runs/${id}/events/history?after_id=${afterId}&limit=${limit}`,
    ),

  decideApproval: (
    runId: string,
    stepId: string,
    body: {
      decision: "approve" | "reject" | "edit_args";
      edited_args?: Record<string, unknown>;
      note?: string;
      decided_by?: string;
    },
  ) =>
    request<Approval>(`/api/runs/${runId}/approvals/${stepId}`, {
      method: "POST",
      body: JSON.stringify({ decided_by: "web", ...body }),
    }),

  tools: () =>
    request<{ tools: ToolInfo[]; mcp_servers: Record<string, unknown>[] }>("/api/tools"),
  demoTasks: () => request<DemoTask[]>("/api/demo/tasks"),
  settings: () => request<{ settings: Record<string, unknown> }>("/api/settings"),
  updateSettings: (patch: Record<string, unknown>) =>
    request<{ settings: Record<string, unknown> }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
};

/** Absolute URL for a screenshot path returned by the API. */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  if (path.startsWith("http")) return path;
  return `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
}

export function isTerminal(status: RunStatus): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}


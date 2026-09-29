"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Lock, Plug, RotateCw, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { api, type ToolInfo } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useToastStore } from "@/store";

/** Settings: provider, MCP servers, allowlist, autopilot, and the tool catalogue. */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const push = useToastStore((s) => s.push);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);

  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const toolsQuery = useQuery({ queryKey: ["tools"], queryFn: api.tools });

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.updateSettings(patch),
    onSuccess: () => {
      push({ kind: "success", title: "Settings saved" });
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => push({ kind: "error", title: "Could not save settings", body: (error as Error).message }),
  });

  const refreshMcp = useMutation({
    mutationFn: () => fetch(`${process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"}/api/tools/mcp/refresh`, { method: "POST" }),
    onSuccess: () => {
      push({ kind: "success", title: "MCP servers reconnected" });
      void queryClient.invalidateQueries({ queryKey: ["tools"] });
    },
    onError: () => push({ kind: "error", title: "MCP refresh failed" }),
  });

  const settings = draft ?? settingsQuery.data?.settings ?? {};
  const update = (key: string, value: unknown) => setDraft({ ...settings, [key]: value });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-muted">
          Runtime configuration for the agent. Stored in the database, layered on top of the
          environment.
        </p>
      </header>

      {/* ------------------------------------------------ the non-negotiable */}
      <section className="card border-amber/30 bg-amber/5 p-5">
        <div className="flex items-start gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-amber text-canvas">
            <ShieldCheck className="h-4 w-4" aria-hidden />
          </span>
          <div>
            <h2 className="font-semibold">High-risk actions always require approval</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted">
              Paying, submitting, sending and deleting are gated by the executor, not by this
              page. There is deliberately no switch to turn it off — the enforcement lives in
              the agent loop so no UI, flag or API call can bypass it.
            </p>
          </div>
          <span className="pill ml-auto shrink-0 border-amber/40 bg-amber/10 text-amber">
            <Lock className="h-3 w-3" aria-hidden />
            not configurable
          </span>
        </div>
      </section>

      <div className="grid gap-5 lg:grid-cols-2">
        {/* ------------------------------------------------------ LLM */}
        <section className="card p-5">
          <h2 className="font-semibold">Model</h2>
          <div className="mt-4 space-y-4">
            <div>
              <label className="label" htmlFor="llm-provider">Provider</label>
              <select
                id="llm-provider"
                data-testid="llm-provider"
                value={String(settings.llm_provider ?? "anthropic")}
                onChange={(event) => update("llm_provider", event.target.value)}
                className="input"
              >
                {["anthropic", "openai", "gemini", "scripted"].map((provider) => (
                  <option key={provider} value={provider}>
                    {provider === "scripted" ? "scripted (deterministic demo brain)" : provider}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="llm-model">Model</label>
              <input
                id="llm-model"
                data-testid="llm-model"
                value={String(settings.llm_model ?? "")}
                onChange={(event) => update("llm_model", event.target.value)}
                className="input font-mono text-xs"
              />
            </div>
            <p className="text-xs text-faint">
              API keys are read from the environment, never from this page.
            </p>
          </div>
        </section>

        {/* ------------------------------------------------- behaviour */}
        <section className="card p-5">
          <h2 className="font-semibold">Agent behaviour</h2>
          <div className="mt-4 space-y-4">
            <Toggle
              label="Autopilot for low/medium risk"
              hint="When off (strict mode), medium-risk steps also pause for approval."
              checked={Boolean(settings.auto_approve_low_risk)}
              onChange={(value) => update("auto_approve_low_risk", value)}
              testId="autopilot-toggle"
            />
            <Toggle
              label="Review the plan before it runs"
              hint="Park every run after planning so you can edit it first."
              checked={Boolean(settings.planner_preview)}
              onChange={(value) => update("planner_preview", value)}
            />
            <div>
              <label className="label" htmlFor="approval-timeout">
                Approval timeout (seconds)
              </label>
              <input
                id="approval-timeout"
                type="number"
                min={10}
                max={3600}
                value={Number(settings.approval_timeout_seconds ?? 300)}
                onChange={(event) => update("approval_timeout_seconds", Number(event.target.value))}
                className="input"
              />
              <p className="mt-1 text-xs text-faint">An unanswered approval is auto-rejected.</p>
            </div>
          </div>
        </section>

        {/* -------------------------------------------------- allowlist */}
        <section className="card p-5">
          <h2 className="font-semibold">Domain allowlist</h2>
          <p className="mt-1 text-xs text-muted">
            The browser and HTTP tools refuse to leave these hosts. Use <code>*.example.com</code>
            for subdomains and <code>*</code> to disable the check.
          </p>
          <textarea
            aria-label="Domain allowlist"
            data-testid="allowlist"
            rows={3}
            value={Array.isArray(settings.domain_allowlist) ? (settings.domain_allowlist as string[]).join("\n") : ""}
            onChange={(event) =>
              update(
                "domain_allowlist",
                event.target.value.split(",").map((s) => s.trim()).filter(Boolean),
              )
            }
            className="input mt-3 font-mono text-xs"
          />
        </section>

        {/* ------------------------------------------------------ MCP */}
        <section className="card p-5">
          <div className="flex items-center gap-2">
            <h2 className="font-semibold">MCP servers</h2>
            <button
              type="button"
              onClick={() => refreshMcp.mutate()}
              disabled={refreshMcp.isPending}
              className="btn-ghost btn-sm ml-auto"
            >
              {refreshMcp.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <RotateCw className="h-3.5 w-3.5" aria-hidden />
              )}
              Reconnect
            </button>
          </div>
          <ul className="mt-3 space-y-2">
            {(toolsQuery.data?.mcp_servers ?? []).map((server) => {
              const status = String(server.status);
              return (
                <li key={String(server.name)} className="flex items-center gap-2 text-sm">
                  <Plug className="h-3.5 w-3.5 text-faint" aria-hidden />
                  <span className="font-mono text-xs">{String(server.name)}</span>
                  <span
                    className={cn(
                      "pill ml-auto text-[10px]",
                      status === "connected"
                        ? "border-ok/40 bg-ok/10 text-ok"
                        : "border-amber/40 bg-amber/10 text-amber",
                    )}
                  >
                    {status}
                  </span>
                  <span className="font-mono text-[10px] text-faint">
                    {String(server.tool_count ?? 0)} tools
                  </span>
                </li>
              );
            })}
            {!toolsQuery.data?.mcp_servers?.length ? (
              <li className="text-xs text-faint">
                No MCP servers configured. Set <code>TASKPILOT_MCP_SERVERS</code> to enable them.
              </li>
            ) : null}
          </ul>
        </section>
      </div>

      <section className="card p-5">
        <h2 className="font-semibold">
          Tool catalogue{" "}
          <span className="font-mono text-xs font-normal text-faint">
            ({toolsQuery.data?.tools.length ?? 0})
          </span>
        </h2>
        <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {(toolsQuery.data?.tools ?? []).map((tool) => (
            <div key={tool.name} className="rounded-xl border border-line bg-canvas p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs text-ink">{tool.name}</span>
                <span
                  className={cn(
                    "pill ml-auto text-[10px]",
                    tool.risk_level === "high"
                      ? "border-amber/40 bg-amber/10 text-amber"
                      : tool.risk_level === "medium"
                        ? "border-line2 text-muted"
                        : "border-line text-faint",
                  )}
                >
                  {tool.risk_level}
                </span>
              </div>
              <p className="mt-1 line-clamp-2 text-[11px] text-muted">{tool.description}</p>
              {tool.source === "mcp" ? (
                <span className="mt-1 inline-block font-mono text-[10px] text-brand">
                  via MCP · {tool.server}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {draft ? (
        <div className="sticky bottom-4 flex items-center gap-2 rounded-2xl border border-amber/40 bg-raised/95 p-3 shadow-amber backdrop-blur">
          <span className="text-sm text-muted">You have unsaved changes.</span>
          <button type="button" onClick={() => setDraft(null)} className="btn-ghost btn-sm ml-auto">
            Discard
          </button>
          <button
            type="button"
            onClick={() => save.mutate(draft)}
            disabled={save.isPending}
            className="btn-primary btn-sm"
            data-testid="save-settings"
          >
            {save.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Check className="h-3.5 w-3.5" aria-hidden />
            )}
            Save
          </button>
        </div>
      ) : null}
    </div>
  );
}

function Toggle({ label, hint, checked, onChange, testId }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  testId?: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        data-testid={testId}
        className="mt-0.5 h-4 w-4 rounded border-line2 bg-canvas accent-amber"
      />
      <span>
        <span className="block text-sm font-medium">{label}</span>
        {hint ? <span className="mt-0.5 block text-xs text-faint">{hint}</span> : null}
      </span>
    </label>
  );
}

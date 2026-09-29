import { Globe, Plug, Terminal } from "lucide-react";

import { TaskInput } from "@/components/task-input";

const FEATURES = [
  {
    icon: Globe,
    title: "Browser automation",
    body: "Real Chromium, a screenshot after every action, and a per-run isolated context. Selectors are recovered from live observations instead of guessed.",
    tint: "text-amber",
  },
  {
    icon: Plug,
    title: "MCP integration",
    body: "Connect any Model Context Protocol server through the official Python SDK. Remote tools become first-class tools with the same approval gate.",
    tint: "text-brand",
  },
  {
    icon: Terminal,
    title: "HTTP APIs",
    body: "Call JSON endpoints, read the results and write reports. Non-GET requests are classified as medium risk and surface in the audit trail.",
    tint: "text-ok",
  },
];

export default function HomePage() {
  return (
    <div className="space-y-24">
      {/* ---------------------------------------------------------------- hero */}
      <section className="relative overflow-hidden pt-10 sm:pt-16">
        <div
          className="dot-field pointer-events-none absolute inset-0 -z-10 opacity-60"
          aria-hidden
        />
        <div
          className="pointer-events-none absolute left-1/2 top-0 -z-10 h-72 w-[42rem] -translate-x-1/2 rounded-full bg-amber/10 blur-3xl"
          aria-hidden
        />

        <div className="mx-auto max-w-3xl text-center">
          <span className="pill border-amber/40 bg-amber/10 text-amber">
            Autonomous Agents for Everyday Apps
          </span>
          <h1 className="mt-6 text-balance text-5xl font-bold tracking-tight sm:text-6xl">
            Task<span className="text-amber">Pilot</span>
          </h1>
          <p className="mt-3 text-lg font-medium text-muted">
            Plan. Act. Pause. <span className="text-ink">(For you.)</span>
          </p>
          <p className="mx-auto mt-4 max-w-xl text-balance text-[15px] leading-relaxed text-muted">
            Give it a task in plain English. It plans the steps, drives real web apps,
            and stops for your approval before anything irreversible happens.
          </p>
        </div>

        <div className="mx-auto mt-10 max-w-2xl">
          <TaskInput />
        </div>
      </section>

      {/* ------------------------------------------------------------ features */}
      <section aria-labelledby="features-heading">
        <h2 id="features-heading" className="sr-only">
          What TaskPilot can do
        </h2>
        <div className="grid gap-4 sm:grid-cols-3">
          {FEATURES.map((feature) => {
            const Icon = feature.icon;
            return (
              <article
                key={feature.title}
                className="card card-hover p-5"
              >
                <span
                  className={`grid h-9 w-9 place-items-center rounded-xl border border-line bg-raised ${feature.tint}`}
                >
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <h3 className="mt-4 font-semibold">{feature.title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-muted">{feature.body}</p>
              </article>
            );
          })}
        </div>
      </section>

      {/* -------------------------------------------------------- why it matters */}
      <section
        aria-labelledby="why-heading"
        className="card relative overflow-hidden p-6 sm:p-10"
      >
        <div
          className="dot-field-faint pointer-events-none absolute inset-0 opacity-40"
          aria-hidden
        />
        <div className="relative max-w-2xl">
          <h2 id="why-heading" className="text-2xl font-semibold tracking-tight">
            Why it matters
          </h2>
          <p className="mt-3 text-[15px] leading-relaxed text-muted">
            An agent that can buy things is only useful if a human still holds the
            wallet. TaskPilot treats the pause as the feature, not an interruption:
            risk is classified by deterministic rules, a high-risk step can never run
            without a recorded approval, and every run leaves an immutable audit trail
            you can replay.
          </p>
          <ul className="mt-6 space-y-3 text-sm">
            {[
              "The executor blocks — enforcement lives in the agent loop, not the UI.",
              "Timeout is a decision: an unanswered approval is an explicit auto-reject.",
              "A rejection stops the run. No later step assumes the one you declined.",
              "Page content is treated as untrusted data, never as instructions.",
            ].map((line) => (
              <li key={line} className="flex gap-3 text-muted">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber" aria-hidden />
                {line}
              </li>
            ))}
          </ul>
        </div>
      </section>
    </div>
  );
}

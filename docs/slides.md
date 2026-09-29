# TaskPilot — 10-slide outline

Team NeuroNova · Autonomous Agents for Everyday Apps.
Each slide lists the headline, the content, and the visual.

---

## 1 — Title

**TaskPilot**
Plan. Act. Pause. (For you.)

- Subtitle: *An AI agent that acts across real web apps — and stops for your approval
  before anything irreversible.*
- Team NeuroNova · hackathon build
- **Visual:** the landing hero on a near-black canvas, amber accent, pixel-dot field.

---

## 2 — The problem

**Agents are fast. They are also trusted with too much.**

- Today's agents can click, buy, and submit — with no gate between "decide" and "do".
- The failure isn't a bad plan. It's a good plan executed at the wrong moment.
- Every incident review ends with the same question: *did a human know?*
- **Visual:** two columns — "what the agent decided" vs "what the human saw".

---

## 3 — What we built

**An agent with a real gate — not a confirmation dialog.**

- Takes a plain-language task; plans ordered steps; drives browser, MCP and HTTP.
- Classifies each action's risk with deterministic rules.
- **Blocks** before anything irreversible, and waits for a recorded human decision.
- Writes an immutable, replayable audit trail for every run.
- **Visual:** the three-leg diagram (Plan → Act → Pause) with the gate on the third.

---

## 4 — The gate is not a UI feature

**The executor cannot proceed without a decision.**

```
decision = policy.classify(step, tool)
if decision.needs_approval:
    outcome = await broker.request(...)   # blocks
    if not outcome.granted: return SKIPPED
result = await tool.run(args)             # only reachable after the gate
```

- There is no path to a tool call that skips the branch.
- Risk is `max(deterministic rules, model's own claim)` — an optimistic model cannot
  wave through a payment.
- Timeout is a decision: an unanswered approval is an explicit **auto-reject**.
- **Visual:** the code snippet, with the gate line highlighted.

---

## 5 — It shows its work

**The approval card is the product.**

- The action, the target URL, the **exact arguments** — and they are editable.
- A screenshot of the live page, taken at request time.
- "Why TaskPilot paused": the specific rules that fired, in plain English.
- A countdown to the auto-reject.
- **Visual:** the real approval card, annotated with four callouts.

---

## 6 — Read-only shouldn't be interrupted

**Risk is a property of effect, not vocabulary.**

- "Open the checkout page" is not "place the order". Read-only tools are exempt from
  keyword escalation; a non-submitting form fill caps at medium.
- Structural rules — submit flags, HTTP verbs, file overwrites — are never capped.
- Result: **exactly one gate per risky task, zero on read-only ones.**
- **Visual:** a table, left = over-gating (before), right = correct (after).

---

## 7 — Three ways to act

**Browser · MCP · APIs — one interface, one gate.**

- **Browser:** real Chromium, per-run isolated context, a screenshot after every action.
- **MCP:** the official Python SDK; remote tools become first-class tools with the
  same risk classification.
- **APIs:** any JSON endpoint; non-GET is medium risk and shows up in the audit trail.
- **Visual:** the tool catalogue from `/api/tools`, grouped by source.

---

## 8 — The numbers

**15 tasks, scored against the demo store's own state.**

| Metric | Result |
|---|---|
| Task success | **15/15 (100%)** |
| Step-completion accuracy | 92% |
| Approval-gate correctness | **100%** |
| **Risky actions without approval** | **0** |
| Time saved | **42.8 min** (35.5× faster) |

- The harness checks MockShop's *world state* before and after — never the agent's
  own summary. A breach fails the run outright.
- **Visual:** `eval/charts/results.png` (success · gate correctness · time per task).

---

## 9 — Honest limits

**What we did not build.**

- A paused run is an in-memory task; a restart marks it `interrupted` (trail survives).
- Single-process execution; a DB poll makes multi-worker *work*, not *owned*.
- The demo brain is deterministic — demo numbers are a reliability **floor**, not a
  model-quality claim.
- No auth or multi-tenancy; MCP is stdio/SSE, one session per server.
- **Visual:** the limitations list, plainly typeset. Confidence costs credibility.

---

## 10 — Close

**Plan. Act. Pause. (For you.)**

- The hard part isn't an agent that can click.
- It's an agent you can let near your credit card.
- **TaskPilot** — TaskPilot proves the pause holds.
- Repo · live demo · `make eval` to reproduce the numbers.
- **Visual:** the completion card, then the landing hero.

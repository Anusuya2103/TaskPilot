# TaskPilot — Architecture

This document explains *why* the system is shaped the way it is. For the diagram and
setup, see the [README](../README.md).

---

## 1. The central constraint

TaskPilot can spend money and submit forms. That single fact drives every design
decision below. The requirement is not "ask the user nicely" — it is:

> **No irreversible action may execute without a recorded, human decision that
> survived a process boundary.**

The tempting implementation is to pause in the UI and set a flag. That fails in three
ways: the UI can be closed, a plan can be edited after approval, and a second
client can call the API directly. So the gate lives in the only place that can
actually prevent a tool call: **the executor's step loop**.

```python
# app/agent/executor.py — the shape of the guarantee
decision = self.policy.classify(step, tool, settings=self.settings)
if decision.needs_approval:
    approval = await self._gate_step(...)      # blocks; returns a decision
    if not approval.granted:
        return skipped_step                     # no tool call happens
    args = approval.args or args               # the *approved* args are used
result = await tool.run(args, ctx)             # only reachable after the gate
```

Nothing below this comment can run a gated action without a decision, because there
is no path to `tool.run` that skips the `if`.

---

## 2. Layering

```
        API routes  (FastAPI)                 ← HTTP shape, auth, rate limit
              │
        Orchestrator                           ← the only component that knows
              │                                 about both the DB and the agent
      ┌───────┼────────┬───────────┐
   Planner  Policy  Executor    Verifier       ← the agent, no I/O of its own
      │       │        │            │
      └───────┴────────┴─────��─────┘
                       │
                 Tool interface                ← one ABC, many capabilities
          ┌────────┬──────���─────┬──────────┐
      Browser    HTTP        Files       MCP     ← remote tools use the same ABC
                       │
                  target sites
```

The dependency arrow only ever points down. The agent never imports a router; tools
never import the orchestrator. This is what makes the executor testable in
milliseconds — `RunHooks` replaces every side effect with an append-only list.

### Why `RunHooks` exists

The executor needs to emit ~12 kinds of event. Injecting them as a struct of async
callbacks (rather than calling the DB directly) means:

- the retry/replan/gate tests need no database and no event bus;
- the orchestrator implements the hooks **once**, against both the DB *and* the bus,
  so the audit trail and the live SSE feed are structurally incapable of disagreeing;
- a tool that logs twice cannot produce two audit rows, because it has no logging.

---

## 3. Risk classification: two opinions, max wins

`app/agent/policy.py` computes:

```
effective_risk = max(deterministic_rule_risk, planner_declared_risk, optional_llm_opinion)
```

The deterministic rules look at **effect shape**, not vocabulary:

| Rule | Example | Risk |
|---|---|---|
| `payment_terms` | "buy", "pay", "place order", "checkout" | high |
| `destructive_terms` | "delete", "unsubscribe", "overwrite" | high |
| `submit_control` | selector matches `#place-order`, `[type=submit]` | high |
| `submit_flag` | `browser_fill_form(..., submit=True)` | high |
| `http_delete` | method is `DELETE` | high |
| `risky_endpoint` | non-GET to `/checkout`, `/payments` | high |
| `file_overwrite` | writing a path that already exists | high |
| `outbound_terms` | "send", "submit", "publish" | high / medium |
| `http_mutating_method` | `POST`/`PUT`/`PATCH` | medium |

Every fired rule is stored with a human-readable `detail`, which is what the approval
card renders under *"Why TaskPilot paused"*. The gate shows its work.

### The read-only exemption — and why it matters

A first implementation gated "Open the checkout page" because the word *checkout*
appeared. That is wrong: **risk is a property of the effect, not the description.**

```python
# app/tools/base.py
read_only = True                       # browser_open cannot change anything
def keyword_risk_ceiling(self, args) -> RiskLevel:
    return RiskLevel.LOW if self.read_only else RiskLevel.HIGH
```

Read-only tools are exempt from keyword escalation; a non-submitting form fill caps
at medium. Structural rules (submit flags, HTTP verbs, file overwrites) are *not*
capped — they describe the effect directly. Result: exactly one gate per risky task,
and zero gates on read-only ones. The evaluation harness measures both directions.

### High risk is not configurable

```python
# PolicyEngine.approval_required
if risk is RiskLevel.HIGH:
    return True      # no setting can make this False
```

`TASKPILOT_AUTO_APPROVE_LOW_RISK=false` puts *medium*-risk steps into strict mode.
There is no way to disable the high-risk gate, in env, API or UI — the Settings page
says so explicitly rather than showing a switch that does nothing.

---

## 4. The approval rendezvous

Two wake-up channels, because a run is owned by one process but the API may not be:

1. **In-process `asyncio.Event`** — the fast path (sub-millisecond).
2. **Database poll** (0.75 s) — so a decision written by a *different* worker still
   unblocks the run.

Ordering is what makes it safe: the approval row is committed **before** the broker
is woken, so the DB poll and the in-process event can never report different
outcomes. On timeout the pending row is flipped to `timed_out` with
`decided_by = "system"` — an unanswered approval is an explicit *reject*, never a
silent "assume approved".

### Editing arguments

`edit_args` is a first-class decision, not a bolt-on. The edited args are what the
tool receives, and because the executor **re-derives risk from the actual args**
immediately before calling, an approval cannot be used to smuggle in a different,
riskier action: edit the selector to something else and the policy runs again.

---

## 5. Verification: cheap first, model only when needed

```
tool result
   ├─ tool error?                → fail (no LLM call)
   ├─ HTTP >= 400?               → fail
   ├─ error markers on the page? → fail
   ├─ price named in expected?  → must be present in the observation
   ├─ count named in expected?  → exact only if phrased as exact
   ├─ empty result?              → fail unless the step permits empty
   └─ otherwise                  → ask the LLM (if configured)
```

Two lessons from the e2e tests are baked in:

- **"at least one" is not "exactly one."** Treating a lower bound as an exact count
  failed a step that had found three matching items.
- **`count: 0` does not mean "the page is empty."** It is the length of the scraped
  list. Only a step that was actually collecting a list can be empty, plus explicit
  empty-state markers in the page text.

A step is never marked successful because a model was *polite* about it: the
deterministic pass is authoritative for failure.

---

## 6. Prompt-injection defence

Two independent layers, because one is not enough:

1. **Sanitisation** (`app/core/security.py`) — eight patterns (ignore-previous,
   role-switch, secret exfiltration, tool hijack, approval bypass, payment
   redirection, exfil URL, instruction markers) plus zero-width/invisible-character
   stripping. Offending *lines* are removed and replaced with a marker before text
   ever reaches a prompt.
2. **Delimitation** — surviving text is wrapped in
   `<<<UNTRUSTED_DATA … UNTRUSTED_DATA>>>`, and the system prompts state that content
   inside those markers is evidence, never instruction.

Plus a **domain allowlist** checked at the tool boundary (not the UI), so a hijacked
plan cannot reach hosts the operator has not allowlisted.

---

## 7. Events: one append, two consumers

`RunEvent.id` is a monotonic integer that doubles as the SSE cursor. Resumption is
therefore a schema-level guarantee, not a runtime hack:

```
                                            ├─ replay WHERE id > 142   (backlog)
                                            └─ live queue, de-duplicated on id
```

Backlog and live are joined at a single cursor, so a client that reconnects mid-run
sees neither a gap nor a duplicate. The UI reports "reconnecting" while the
underlying mechanism is invisible.

The orchestrator writes the row and publishes the frame in the **same** call, so the
audit trail and the live feed cannot drift.

---

## 8. Data model

```
tasks 1---* runs 1---* steps
                 ├---* approvals      (one per gated step; immutable once decided)
                 +---* screenshots   (one per browser action)
                 +---* events        (append-only; id == SSE cursor)
```

Enums are stored as strings with Python-side `str` enums — identical on SQLite and
Postgres, and no enum migrations. `Step.args` carries `__plan_step_id` so the API can
address a step as `s1` while the primary key stays a UUID; the UI never sees UUIDs.

---

## 9. Demo mode

`ScriptedLLM` implements the same `LLMClient` interface and returns real plans for
known task shapes. It is **not** a stub that fakes outcomes: the tools, the policy
engine, the gate, the verifier and the event log are all the production path.

It exists because a demo must not depend on API credits or a live site. When no key
is configured, `build_llm` logs a warning and falls back, and `/ready` reports the
*effective* provider so the fallback is never mistaken for a real model.

---

## 10. Deliberate non-goals

- **No durable execution queue.** A paused run is an in-memory task; restarts mark it
  `interrupted`. The event log is the raw material for a resumable worker, but that
  worker is not built.
- **No multi-tenancy or user accounts.** Optional `X-API-Key`, one namespace.
- **No learned risk model.** The policy is deliberately shallow, explainable and
  deterministic. A classifier would be more accurate and far less auditable.

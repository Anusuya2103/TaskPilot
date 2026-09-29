# TaskPilot — 3-minute demo video script

**Runtime:** 3:00 · **Tone:** confident, specific, no hype.
**Rule:** every claim on screen must be a number we actually measured.

---

## Shot list

### 0:00 – 0:20 · Cold open: the problem

**Frame:** Landing page, static. Cursor resting on the task input.

**Voiceover:**
> "Every agent demo ends the same way: it does something impressive, and then it
> spends your money without asking. TaskPilot is built around the opposite claim —
> the agent can act, but it *pauses* before anything that can't be undone."

**On-screen text:** `Plan. Act. Pause. (For you.)`

---

### 0:20 – 0:45 · Run the task

**Frame:** Click the "Buy the cheapest USB-C cable" chip. The prompt fills. Click **Run task**.

**Voiceover:**
> "One sentence. No selectors, no scripting."

**Cut to:** the run page. Left: the step timeline filling in. Right: the terminal
streaming. The browser screenshot panel updating after every action.

**Voiceover:**
> "It searches, reads the prices, picks the cheapest, opens the product, adds it to the
> cart, and fills the checkout — without once guessing a selector it hadn't seen.
> Every action is screenshotted. Notice there is no approval yet: reading, navigating
> and filling fields are reversible, so the agent doesn't interrupt you."

**Callout on screen:** a small label reading **"not gated — reversible"** on the earlier steps.

---

### 0:45 – 1:15 · The pause (the money shot)

**Frame:** The amber approval card slides in. Camera holds. **Let the countdown tick.**

**Voiceover:**
> "Here is the whole product in one frame. It reached the payment step and *stopped*.
> Not because the UI asked it to — because the executor is blocked waiting on a human
> decision, and it cannot proceed without one."

**On screen, pointed at:**
- the target URL and the exact arguments (`#place-order`),
- the screenshot of the live checkout page,
- *"Why TaskPilot paused"* — the policy rules that fired,
- the countdown, and the note that it auto-rejects at zero.

**Voiceover:**
> "The reasons aren't vague. A deterministic rule engine classified this: the
> selector matches a submit control, and the step contains payment language. And
> note the 'pay' in this step — the model that planned this marked it *low* risk.
> The rules don't listen to the model."

---

### 1:15 – 1:45 · Approve and finish

**Frame:** Click **Approve & continue**. The card dismisses. The run completes.

**Voiceover:**
> "One click. The agent resumes, places the order, reads back the confirmation, and
> tells me what it did."

**Cut to:** the completion card — "Task completed!", the final answer, and the stats row.

**Voiceover:**
> "And it shows its work: how many steps succeeded, how long it took against the
> manual baseline, and how many approvals were involved. The report is downloadable,
> and the audit log behind it is immutable."

**Optional beat (fast):** click **Cancel** on a second run instead, and show the run
stopping with *"You declined step s8. The agent stopped there."*

---

### 1:45 – 2:20 · The proof

**Frame:** `backend/eval/results.md` in the editor, or the chart.

**Voiceover:**
> "We didn't trust the agent's own summary. The evaluation harness runs fifteen tasks
> against a demo store and checks MockShop's *own state* before and after each one."

**On screen, reading the numbers:**
- **15/15 tasks succeeded**
- **100% approval-gate correctness**
- **zero** risky actions executed without approval
- **42.8 minutes** saved against the manual baseline

**Voiceover:**
> "A task only counts as gate-correct if it paused exactly when it should have — and
> if nothing marked 'needs approval' ever reached 'done' without a recorded approval.
> That last number is zero, and it isn't zero by luck. It's enforced in the agent
> loop, and there are tests that try to break it."

---

### 2:20 – 2:50 · The one thing to remember

**Frame:** Back to the approval card, static, zoomed slightly.

**Voiceover:**
> "Anyone can build an agent that clicks a button. The hard part is building one you
> can actually let near your credit card. TaskPilot treats the pause as the feature —
> and proves it holds."

**On-screen text:** `Plan. Act. Pause. (For you.)`

---

### 2:50 – 3:00 · Close

**Frame:** Landing page, then the repo URL.

**Voiceover:**
> "TaskPilot. Plan, act, pause — for you."

---

## Production notes

- **Pre-run:** `docker compose up`, open MockShop, reset it via `/admin/reset` so the
  demo starts from a known state.
- **Rehearse the timing.** The pause lands at ~0:50. If the run is faster on stage,
  slow the voiceover rather than rushing the approval card.
- **Have a backup tab** on `eval/results.md` in case the demo computer struggles with
  two servers.
- **Disable notifications** in the recording environment; the approval sound and toast
  fire for real.

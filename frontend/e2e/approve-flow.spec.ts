import { expect, test } from "@playwright/test";

/**
 * The approve flow, end to end through the real UI.
 *
 * This is the test that proves the product's claim from the *user's* side: a
 * risky action pauses, nothing is ordered until a human clicks Approve, and the
 * run only completes afterwards.
 *
 * Requires the API to be configured with the deterministic scripted brain
 * (see playwright.config.ts) and MockShop to be reachable.
 */

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8010";

/** Reset the demo store so the run starts from a known, empty state. */
async function resetMockShop() {
  const base = process.env.E2E_MOCKSHOP_URL ?? "http://127.0.0.1:9000";
  try {
    await fetch(`${base}/admin/reset`);
  } catch {
    // MockShop may not be running in a unit-only CI job; the assertions below
    // will fail loudly if the run cannot reach it, which is the point.
  }
}

async function waitForRunState(
  runId: string,
  predicate: (run: Record<string, unknown>) => boolean,
  timeoutMs = 90_000,
) {
  const deadline = Date.now() + timeoutMs;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    const response = await fetch(`${API}/api/runs/${runId}`);
    if (response.ok) {
      last = await response.json();
      if (predicate(last)) return last;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`run ${runId} never reached the expected state (last: ${last.status})`);
}

const isPaused = (run: Record<string, unknown>) =>
  run.status === "awaiting_approval" && Boolean(run.pending_approval);

const isTerminal = (run: Record<string, unknown>) =>
  ["completed", "failed", "cancelled"].includes(String(run.status));

test.describe("approval gate", () => {
  test.slow();
  test.beforeEach(async () => {
    await resetMockShop();
  });

  test("pauses before paying, then completes after approval", async ({ page }) => {
    // --- submit the task through the UI ---------------------------------
    await page.goto("/");
    await expect(page.getByRole("heading", { name: /TaskPilot/i })).toBeVisible();

    await page.getByTestId("demo-chip-mockshop-buy-cable").click();
    await expect(page.getByTestId("task-input")).toHaveValue(/cheapest USB-C cable/i);

    await page.getByTestId("run-task").click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/, { timeout: 20_000 });

    const runId = page.url().split("/").pop() as string;

    // --- it must pause ---------------------------------------------------
    await expect(page.getByTestId("approval-card")).toBeVisible({ timeout: 90_000 });
    await expect(page.getByTestId("run-status")).toContainText(/awaiting approval/i);

    // The card shows what is about to happen and why.
    await expect(page.getByTestId("approval-card")).toContainText(/cannot be undone/i);
    await expect(page.getByTestId("args-preview")).toContainText("place-order");
    await expect(page.getByTestId("approval-countdown")).toBeVisible();

    // Nothing has been ordered yet.
    const paused = await waitForRunState(runId, isPaused);
    expect(paused.pending_approval).toBeTruthy();
    const stepId = (paused.pending_approval as Record<string, unknown>).step_id as string;

    // --- approve ---------------------------------------------------------
    await page.getByTestId("approval-approve").click();

    const done = await waitForRunState(runId, isTerminal);
    expect(done.status).toBe("completed");

    // The run really did wait for the human: exactly one approval, granted.
    const summary = await (await fetch(`${API}/api/runs/${runId}/summary`)).json();
    expect(summary.approvals_requested).toBe(1);
    expect(summary.approvals_granted).toBe(1);
    expect(summary.risky_actions_executed_without_approval).toBe(0);

    // The gated step carries the approval timestamp.
    const steps = summary.steps as Array<Record<string, unknown>>;
    const gated = steps.filter((s) => s.needs_approval);
    expect(gated).toHaveLength(1);
    expect(gated[0].approved_at).toBeTruthy();

    // --- completion UI ---------------------------------------------------
    await expect(page.getByTestId("completion-card")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("completion-card")).toContainText(/Task completed/i);
    await expect(page.getByTestId("step-timeline")).toBeVisible();

    // The audit log is reachable and lists the approval.
    await page.goto(`/audit/${runId}`);
    await expect(page.getByTestId("audit-table")).toContainText("approval_requested");
    await expect(page.getByTestId("audit-table")).toContainText("approval_decision");
  });

  test("rejecting stops the run and orders nothing", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("demo-chip-mockshop-buy-cable").click();
    await page.getByTestId("run-task").click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/, { timeout: 20_000 });
    const runId = page.url().split("/").pop() as string;

    await expect(page.getByTestId("approval-card")).toBeVisible({ timeout: 90_000 });
    await page.getByTestId("approval-reject").click();

    const done = await waitForRunState(runId, isTerminal);
    expect(done.status).toBe("failed");
    expect(String((done.error as Record<string, string>)?.message)).toMatch(/declined/i);

    const summary = await (await fetch(`${API}/api/runs/${runId}/summary`)).json();
    expect(summary.approvals_rejected).toBe(1);
    // The world must be untouched: no risky step ran.
    expect(summary.risky_actions_executed_without_approval).toBe(0);
  });

  test("a read-only task never interrupts", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("demo-chip-mockshop-read-dashboard").click();
    await page.getByTestId("run-task").click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/, { timeout: 20_000 });
    const runId = page.url().split("/").pop() as string;

    const done = await waitForRunState(runId, isTerminal);
    expect(done.status).toBe("completed");
    expect(done.approvals_count).toBe(0);
    // The gate must not fire for a harmless read.
    await expect(page.getByTestId("approval-card")).toHaveCount(0, { timeout: 5000 });
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApprovalCard } from "@/components/approval-card";
import { api, type Approval } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, decideApproval: vi.fn() } };
});

// `api` above is the mocked module, so this picks up the spy from vi.mock.
const decide = vi.mocked(api.decideApproval);

function makeApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "ap-1",
    run_id: "run-1",
    step_id: "step-uuid",
    status: "pending",
    reason: "payment language",
    risk: "high",
    risk_reasons: [
      { rule: "payment_terms", detail: "payment/purchase language “pay”", risk: "high" },
    ],
    preview: {
      action: "Place the order and pay",
      tool: "browser_click",
      tool_description: "Click an element",
      args: { selector: "#place-order" },
      args_preview: '{"selector": "#place-order"}',
      risk: "high",
      declared_risk: "low",
      rule_risk: "high",
      reasons: [],
      explanation: "payment/purchase language",
      target: "#place-order",
      current_url: "http://localhost:9000/checkout",
      page_title: "Checkout",
      screenshot: null,
      reversible: false,
      consequence: "This action cannot be undone by TaskPilot.",
    },
    original_args: { selector: "#place-order" },
    edited_args: null,
    screenshot_url: null,
    requested_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 120_000).toISOString(),
    decided_at: null,
    decided_by: null,
    decision_note: null,
    seconds_remaining: 120,
    ...overrides,
  };
}

function renderCard(approval: Approval = makeApproval()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ApprovalCard runId="run-1" stepId="s8" approval={approval} />
    </QueryClientProvider>,
  );
}

describe("ApprovalCard", () => {
  beforeEach(() => {
    // The spy is module-level, so calls would otherwise leak between tests.
    decide.mockReset();
  });

  it("shows what is about to happen, and why it paused", () => {
    renderCard();
    expect(screen.getByTestId("approval-card")).toBeInTheDocument();
    expect(screen.getByText("Place the order and pay")).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
    expect(screen.getByText(/payment\/purchase language/i)).toBeInTheDocument();
    // the exact arguments, not a paraphrase
    expect(screen.getByTestId("args-preview")).toHaveTextContent("#place-order");
  });

  it("renders a countdown that warns as it approaches the auto-reject", () => {
    renderCard();
    const countdown = screen.getByTestId("approval-countdown");
    expect(countdown).toBeInTheDocument();
    expect(countdown.textContent).toMatch(/\d:\d{2}/);
  });

  it("approves with the plan step id and the exact args", async () => {
    decide.mockResolvedValue(makeApproval({ status: "approved" }));
    renderCard();
    await userEvent.click(screen.getByTestId("approval-approve"));
    await waitFor(() => expect(decide).toHaveBeenCalledTimes(1));
    expect(decide).toHaveBeenCalledWith("run-1", "s8", { decision: "approve" });
  });

  it("rejects without sending any args", async () => {
    decide.mockResolvedValue(makeApproval({ status: "rejected" }));
    renderCard();
    await userEvent.click(screen.getByTestId("approval-reject"));
    await waitFor(() =>
      expect(decide).toHaveBeenCalledWith("run-1", "s8", { decision: "reject" }),
    );
  });

  it("surfaces a JSON validation error instead of sending a broken edit", async () => {
    decide.mockResolvedValue(makeApproval({ status: "edited" }));
    renderCard();
    await userEvent.click(screen.getByTestId("edit-args"));
    const editor = screen.getByTestId("args-editor");
    fireEvent.change(editor, { target: { value: "{not json" } });
    await userEvent.click(screen.getByTestId("approval-edit-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/valid JSON/i);
    expect(decide).not.toHaveBeenCalled();
  });

  it("sends the edited args when they parse", async () => {
    decide.mockResolvedValue(makeApproval({ status: "edited" }));
    renderCard();
    await userEvent.click(screen.getByTestId("edit-args"));
    const editor = screen.getByTestId("args-editor");
    fireEvent.change(editor, { target: { value: '{"selector": "#place-order-2"}' } });
    await userEvent.click(screen.getByTestId("approval-edit-submit"));
    await waitFor(() => expect(decide).toHaveBeenCalled());
    expect(decide.mock.calls[0][2]).toMatchObject({
      decision: "edit_args",
      edited_args: { selector: "#place-order-2" },
    });
  });

  it("disables the buttons once the approval has expired", () => {
    renderCard(makeApproval({ expires_at: new Date(Date.now() - 1000).toISOString() }));
    expect(screen.getByTestId("approval-approve")).toBeDisabled();
    expect(screen.getByTestId("approval-reject")).toBeDisabled();
  });
});

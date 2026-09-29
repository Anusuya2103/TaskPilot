"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { useToastStore } from "@/store";

/**
 * The hero task input.
 *
 * Optimistic on the chip clicks (the prompt fills instantly, the request is
 * only sent on submit) and explicit about what the agent will and will not do
 * without asking, because that is the product's whole promise.
 */
export function TaskInput() {
  const router = useRouter();
  const push = useToastStore((s) => s.push);
  const [prompt, setPrompt] = useState("");
  const [reviewPlan, setReviewPlan] = useState(false);

  const { data: demos } = useQuery({
    queryKey: ["demo-tasks"],
    queryFn: api.demoTasks,
    staleTime: 300_000,
    retry: false,
  });

  const start = useMutation({
    mutationFn: () => api.createTask({ prompt: prompt.trim(), review_plan: reviewPlan }),
    onSuccess: (run) => {
      push({ kind: "success", title: "Task accepted", body: "Watch it plan and act." });
      router.push(`/runs/${run.id}`);
    },
    onError: (error) => {
      const apiError = error as ApiError;
      push({
        kind: "error",
        title: "Could not start the task",
        body: apiError.message,
      });
    },
  });

  const canSubmit = prompt.trim().length >= 3 && !start.isPending;

  return (
    <div className="w-full">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) start.mutate();
        }}
        className="card overflow-hidden p-2 shadow-amber/20"
      >
        <label htmlFor="task-prompt" className="sr-only">
          Describe the task you want TaskPilot to do
        </label>
        <textarea
          id="task-prompt"
          data-testid="task-input"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && canSubmit) {
              event.preventDefault();
              start.mutate();
            }
          }}
          rows={3}
          placeholder="Find the cheapest USB-C cable on MockShop, add it to the cart, and buy it — stop before you pay."
          aria-describedby="task-hint"
          className="w-full resize-y rounded-xl bg-transparent px-4 py-3 text-[15px] leading-relaxed text-ink placeholder:text-faint focus:outline-none"
        />
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-3 py-2.5">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={reviewPlan}
              onChange={(event) => setReviewPlan(event.target.checked)}
              data-testid="review-plan-toggle"
              className="h-3.5 w-3.5 rounded border-line2 bg-canvas accent-amber"
            />
            Review the plan before it runs
          </label>
          <span id="task-hint" className="hidden text-xs text-faint sm:inline">
            <span className="kbd">⌘</span> <span className="kbd">↵</span> to run
          </span>
          <button
            type="submit"
            disabled={!canSubmit}
            data-testid="run-task"
            className="btn-primary ml-auto"
          >
            {start.isPending ? "Starting…" : "Run task"}
          </button>
        </div>
      </form>

      {demos && demos.length > 0 ? (
        <div className="mt-5">
          <p className="mb-2.5 text-xs uppercase tracking-wider text-faint">
            Try one of these
          </p>
          <div className="flex flex-wrap gap-2">
            {demos.map((task) => (
              <button
                key={task.id}
                type="button"
                data-testid={`demo-chip-${task.id}`}
                onClick={() => {
                  setPrompt(task.prompt);
                  // Keep focus in the textarea so Enter runs immediately.
                  document.getElementById("task-prompt")?.focus();
                }}
                className="chip"
                title={task.description || task.prompt}
              >
                {task.needs_approval ? (
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-amber"
                    aria-label="will ask for approval"
                  />
                ) : (
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-ok"
                    aria-label="no approval needed"
                  />
                )}
                {task.title}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

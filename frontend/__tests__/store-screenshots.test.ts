import { beforeEach, describe, expect, it } from "vitest";

import type { RunEvent } from "@/lib/api";
import { useRunStore } from "@/store";

function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    id: 1,
    run_id: "run-1",
    step_id: null,
    type: "log",
    message: "something happened",
    payload: {},
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const shot = (id: number, src: string) =>
  makeEvent({
    id,
    type: "screenshot",
    message: "screenshot",
    payload: { src, url: "http://mockshop:9000/cart", label: `${src}.png` },
  });

/** `pushEvent` takes a LogLine shape (camelCase `stepId`), not the API's `RunEvent`. */
function toLiveEvent(event: RunEvent) {
  return {
    id: event.id,
    type: event.type,
    message: event.message,
    stepId: event.step_id,
    payload: event.payload,
  };
}

describe("run store screenshots", () => {
  beforeEach(() => {
    useRunStore.getState().attach("run-1");
  });

  it("collects screenshots from the live event stream", () => {
    useRunStore.getState().pushEvent(toLiveEvent(shot(1, "/media/screenshots/a.png")));

    expect(useRunStore.getState().screenshots).toHaveLength(1);
    expect(useRunStore.getState().screenshots[0].src).toBe("/media/screenshots/a.png");
  });

  it("rehydrates screenshots when history is replayed after a reload", () => {
    // Regression: seedHistory used to rebuild `lines` only, so opening a run
    // that had already finished rendered the browser panel as "0 frames" even
    // though the screenshots existed on the server.
    useRunStore.getState().seedHistory([
      makeEvent({ id: 1, type: "run_started" }),
      shot(2, "/media/screenshots/a.png"),
      shot(3, "/media/screenshots/b.png"),
    ]);

    const { screenshots, lines } = useRunStore.getState();
    expect(screenshots).toHaveLength(2);
    expect(screenshots.map((f) => f.src)).toEqual([
      "/media/screenshots/a.png",
      "/media/screenshots/b.png",
    ]);
    // The log must still replay, unchanged.
    expect(lines).toHaveLength(3);
  });

  it("ignores screenshot events with no src and non-screenshot events", () => {
    useRunStore.getState().seedHistory([
      makeEvent({ id: 1, type: "screenshot", payload: {} }),
      makeEvent({ id: 2, type: "step_completed", payload: { src: "/not-a-frame.png" } }),
    ]);

    expect(useRunStore.getState().screenshots).toEqual([]);
  });

  it("caps replayed frames at the same limit as the live stream", () => {
    const many = Array.from({ length: 80 }, (_, i) => shot(i + 1, `/media/screenshots/f${i}.png`));
    useRunStore.getState().seedHistory(many);

    const { screenshots } = useRunStore.getState();
    expect(screenshots).toHaveLength(60);
    // Keeps the *newest* frames, matching MAX_FRAMES trimming on push.
    expect(screenshots.at(-1)?.src).toBe("/media/screenshots/f79.png");
  });
});

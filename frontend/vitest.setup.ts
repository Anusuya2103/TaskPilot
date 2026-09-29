import "@testing-library/jest-dom/vitest";

// jsdom has no EventSource; the SSE client degrades gracefully without it.
if (typeof globalThis.EventSource === "undefined") {
  globalThis.EventSource = class {
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    addEventListener() {}
    removeEventListener() {}
    close() {}
  } as unknown as typeof EventSource;
}

if (typeof globalThis.Notification === "undefined") {
  globalThis.Notification = class {
    static permission = "denied";
    constructor(public title: string) {}
  } as unknown as typeof Notification;
}

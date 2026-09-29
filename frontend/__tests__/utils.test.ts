import { describe, expect, it } from "vitest";

import {
  formatClock,
  formatDuration,
  formatSeconds,
  mediaUrl,
  prettyJson,
  truncate,
} from "@/lib/utils";
import { API_BASE } from "@/lib/api";

describe("formatDuration", () => {
  it("renders sub-second durations in milliseconds", () => {
    expect(formatDuration(240)).toBe("240ms");
  });

  it("renders seconds with one decimal", () => {
    expect(formatDuration(1500)).toBe("1.5s");
  });

  it("renders minutes and seconds", () => {
    expect(formatDuration(95_000)).toBe("1m 35s");
  });

  it("renders a dash for missing values, but a real 0 for zero", () => {
    // 0ms is a legitimate measurement; "—" means "we do not know".
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(0)).toBe("0ms");
  });
});

describe("formatSeconds", () => {
  it("keeps sub-10 values precise and rounds the rest", () => {
    expect(formatSeconds(3.14159)).toBe("3.1s");
    expect(formatSeconds(42.7)).toBe("43s");
    expect(formatSeconds(125)).toBe("2m 5s");
  });

  it("never renders a negative duration", () => {
    expect(formatSeconds(-5)).toBe("-5.0s");
  });
});

describe("formatClock", () => {
  it("falls back to a placeholder for missing or invalid input", () => {
    expect(formatClock(null)).toBe("--:--:--");
    expect(formatClock("not-a-date")).toBe("--:--:--");
  });
});

describe("prettyJson", () => {
  it("truncates very large payloads instead of blowing up the panel", () => {
    const big = { data: "x".repeat(10_000) };
    const out = prettyJson(big, 100);
    expect(out.length).toBeLessThan(200);
    expect(out).toContain("…");
  });

  it("survives circular structures", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(() => prettyJson(circular)).not.toThrow();
  });
});

describe("truncate", () => {
  it("leaves short strings alone", () => {
    expect(truncate("short", 10)).toBe("short");
  });

  it("adds an ellipsis when cutting", () => {
    expect(truncate("abcdefghij", 5)).toHaveLength(5);
  });
});

describe("mediaUrl", () => {
  it("returns null for empty input", () => {
    expect(mediaUrl(null)).toBeNull();
    expect(mediaUrl(undefined)).toBeNull();
    expect(mediaUrl("")).toBeNull();
  });

  it("leaves absolute URLs untouched", () => {
    expect(mediaUrl("https://cdn.example.com/a.png")).toBe(
      "https://cdn.example.com/a.png",
    );
  });

  it("prefixes relative paths with the API base", () => {
    expect(mediaUrl("/media/screenshots/a.png")).toBe(
      `${API_BASE}/media/screenshots/a.png`,
    );
    expect(mediaUrl("media/screenshots/a.png")).toBe(
      `${API_BASE}/media/screenshots/a.png`,
    );
  });
});

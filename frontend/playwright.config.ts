import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config. The approve-flow spec needs the whole stack, so `webServer` starts
 * both the API (with a deterministic scripted brain) and Next.js.
 */
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8010";
const WEB = process.env.E2E_WEB_URL ?? "http://127.0.0.1:3010";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: WEB,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `cd ../backend && ../.venv/bin/python -m uvicorn app.main:get_app --factory --port 8010`,
      url: `${API}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        TASKPILOT_LLM_PROVIDER: "scripted",
        TASKPILOT_MCP_ENABLED: "false",
        TASKPILOT_ALLOW_SCRIPTED_FALLBACK: "true",
        TASKPILOT_DATABASE_URL: "sqlite+aiosqlite:///./e2e.db",
        TASKPILOT_DOMAIN_ALLOWLIST: "localhost,127.0.0.1,mockshop",
        TASKPILOT_LOG_LEVEL: "WARNING",
        NEXT_PUBLIC_API_BASE_URL: API,
      },
    },
    {
      command: "npm run dev -- --port 3010",
      url: WEB,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { NEXT_PUBLIC_API_BASE_URL: API, NEXT_PUBLIC_SSE_BASE_URL: API },
    },
  ],
});

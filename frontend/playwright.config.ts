import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import { join } from "node:path";

/**
 * E2E config. The approve-flow spec needs the whole stack, so `webServer` starts
 * both the API (with a deterministic scripted brain) and Next.js.
 */
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8010";
const WEB = process.env.E2E_WEB_URL ?? "http://127.0.0.1:3010";
const REPO_ROOT = join(__dirname, "..");

/** The venv layout differs between POSIX and Windows. */
function venvPython(): string {
  const posix = join(REPO_ROOT, ".venv/bin/python");
  const windows = join(REPO_ROOT, ".venv/Scripts/python.exe");
  return existsSync(windows) ? windows : posix;
}

/** The eval/demo store must be up for the purchase flows. */
const mockshopPort = Number(process.env.E2E_MOCKSHOP_PORT ?? 9000);

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
      // The demo store. The purchase specs need a real cart/checkout to drive.
      command: `cd ${join(REPO_ROOT, "demo-apps/mockshop")} && ${venvPython()} app.py`,
      url: `http://127.0.0.1:${mockshopPort}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: `cd ${join(REPO_ROOT, "backend")} && ${venvPython()} -m uvicorn app.main:get_app --factory --port 8010`,
      url: `${API}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        TASKPILOT_LLM_PROVIDER: "scripted",
        TASKPILOT_MCP_ENABLED: "false",
        TASKPILOT_ALLOW_SCRIPTED_FALLBACK: "true",
        TASKPILOT_DATABASE_URL: "sqlite+aiosqlite:///./e2e.db",
        TASKPILOT_MOCKSHOP_BASE_URL: `http://127.0.0.1:${mockshopPort}`,
        TASKPILOT_DOMAIN_ALLOWLIST: "localhost,127.0.0.1,mockshop",
        TASKPILOT_LOG_LEVEL: "WARNING",
        // The e2e web server runs on a different port than the default CORS
        // allowlist, so the browser preflight was failing with a 400.
        TASKPILOT_CORS_ORIGINS: `http://127.0.0.1:3010,http://localhost:3010`,
        NEXT_PUBLIC_API_BASE_URL: API,
      },
    },
    {
      command: "npm run dev -- --port 3010",
      url: WEB,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        NEXT_PUBLIC_API_BASE_URL: API,
        NEXT_PUBLIC_SSE_BASE_URL: API,
      },
    },
  ],
});

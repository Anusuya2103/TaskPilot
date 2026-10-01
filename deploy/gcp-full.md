# Deploying TaskPilot to Google Cloud

Everything here needs **your** Google account — I could not authenticate on
your behalf. The configs are written and ready; each step below is one command.

## Read this first: why not one Firebase App Hosting backend

Firebase App Hosting deploys **exactly one Next.js app** from a GitHub repo.
TaskPilot is four services:

| Service | What it is | App Hosting? |
|---|---|---|
| `web` | Next.js 14 | ✅ yes |
| `api` | FastAPI + Playwright/Chromium (~2GB) | ❌ |
| `db` | Postgres 16 | ❌ |
| `mockshop` | FastAPI demo store | ❌ |

So: **App Hosting for the web tier, Cloud Run for the API and MockShop, Cloud
SQL for Postgres.** Still all Blaze-billed, and still one Google login.

### The failure mode that matters

A run waiting for approval is an **in-process `asyncio` task**
(`backend/app/agent/orchestrator.py`, `start_run`). If the API scales to zero
mid-pause, the task is gone and the run becomes `interrupted` — the approval
gate, the one thing a judge will test, silently disappears.

Every deploy below therefore sets **`--min-instances=1 --max-instances=1`** on
the API. Cost while idle is roughly the minimum always-warm instance, which is
acceptable for a submission and vastly better than a broken demo.

## 1 · Prerequisites

```bash
gcloud auth login
gcloud auth application-default login
npm i -g firebase-tools && firebase login
```

The **Blaze (pay-as-you-go) plan is required** — App Hosting and Cloud SQL are
not available on Spark. This will prompt for a billing account and a card.

```bash
gcloud config set project YOUR_PROJECT
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com \
  vpcaccess.googleapis.com cloudresourcemanager.googleapis.com
```

## 2 · Push the repo to GitHub

App Hosting builds from GitHub, so the repo must be public and remote first.

```bash
gh repo create TaskPilot --public --source=. --remote=origin --push
```

## 3 · Deploy the API (Cloud Run)

The API image is `backend/Dockerfile`, built from the **repo root** (it copies
both `backend/` and `demo-apps/`).

```bash
gcloud artifacts repositories create taskpilot \
  --repository-format=docker --location=europe-west1

gcloud builds submit \
  --tag europe-west1-docker.pkg.dev/YOUR_PROJECT/taskpilot/api

export GCP_PROJECT=YOUR_PROJECT
export DB_PASS="$(openssl rand -base64 24)"   # store this; you need it later
chmod +x deploy/cloud-run-api.sh
./deploy/cloud-run-api.sh
```

That script provisions Cloud SQL (Postgres 16, `db-f1-micro`), attaches it, and
deploys with `--min-instances=1`. It prints the API URL.

## 4 · Deploy the web app (App Hosting)

```bash
firebase apphosting:backends:create taskpilot --project YOUR_PROJECT
```

Connect the GitHub repo in the console, then set `apphosting.yaml`:

- `rootDirectory: frontend`
- `NEXT_PUBLIC_API_BASE_URL` → the Cloud Run URL from step 3
- `NEXT_PUBLIC_SSE_BASE_URL` → the same URL

> `NEXT_PUBLIC_*` values are **inlined at build time**, so they must be set
> before the build runs or the browser will call the wrong origin.

## 5 · Deploy MockShop, or accept a demo limitation

MockShop is the target website the agent drives. It needs a reachable URL,
because the API's browser opens it *from outside* the Cloud Run container.

```bash
gcloud run deploy taskpilot-mockshop \
  --image europe-west1-docker.pkg.dev/YOUR_PROJECT/taskpilot/mockshop \
  --region europe-west1 --allow-unauthenticated --min-instances 1
```

Then set `TASKPILOT_MOCKSHOP_BASE_URL` on the API to that URL and redeploy.

**If you skip this**, the agent cannot browse, and the demo degrades to API-only
tool calls — the browser panel stays empty.

## 6 · Verify

- `https://YOUR_WEB_DOMAIN` loads
- a run reaches the amber approval card and stays there
- approving it places **exactly one** MockShop order (`curl YOUR_MOCKSHOP/admin/orders`)
- `curl YOUR_API/ready` reports `mcp: ok`, `db: ok`

## Cheaper and more reliable alternative

A ~€4/mo VPS running `docker compose up -d` as-is is **one URL, no scale-to-zero,
no Cloud SQL, no App Hosting**, and keeps Postgres + MockShop co-located so
`http://mockshop:9000` still resolves. For a hackathon submission this is the
lowest-risk option; use the above if you specifically need a Google-hosted link.
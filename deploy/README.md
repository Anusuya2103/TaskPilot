# TaskPilot — deployment

TaskPilot is four services (`web`, `api`, `db`, `mockshop`); see
[`deploy/gcp-full.md`](deploy/gcp-full.md) for the full Google Cloud walkthrough
and [`deploy/cloud-run-api.sh`](deploy/cloud-run-api.sh) for the API service.

## Local (recommended for reviewers)

```bash
cp .env.example .env
docker compose up
```

- Web `http://localhost:3000` · API `http://localhost:8000` · Swagger `/docs`
- Works on a fresh clone with an empty `.env`: the API falls back to the
  deterministic scripted brain when no LLM key is present.

## Hosted

| Tier | Where | Notes |
|---|---|---|
| `web` | Firebase App Hosting | Next.js 14; needs the Blaze plan |
| `api` | Cloud Run | `--min-instances=1` (see below) |
| `db` | Cloud SQL (Postgres 16) | |
| `mockshop` | Cloud Run | must be publicly reachable for the agent's browser |

> **Why `--min-instances=1`?** A run awaiting approval is an in-process
> `asyncio` task. If the API scales to zero mid-pause, the run is marked
> `interrupted` and the approval gate — the demo's whole point — disappears.
> One always-warm instance prevents it.

Fill the placeholders in [`apphosting.yaml`](apphosting.yaml) with your API URL
before the first build; `NEXT_PUBLIC_*` values are inlined at build time.

A single small VPS running `docker compose up -d` is cheaper and more reliable
than the above for a submission: one URL, no scale-to-zero, Postgres and
MockShop co-located.
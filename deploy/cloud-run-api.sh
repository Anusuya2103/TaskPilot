# Cloud Run deployment for the TaskPilot API.
#
# WHY NOT FIREBASE APP HOSTING
# -----------------------------
# App Hosting deploys exactly one Next.js app from a GitHub repo onto Cloud Run.
# TaskPilot is four services (web / api / db / mockshop) and the API carries a
# ~2GB Playwright+Chromium image that App Hosting's buildpack flow cannot take.
# So this targets Cloud Run directly, with the web app on App Hosting.
#
# USAGE (requires `gcloud auth login` and the Blaze plan):
#   gcloud services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com
#   gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/taskpilot/api
#   ./deploy/cloud-run-api.sh
#
# The critical flag is --min-instances=1. A run awaiting approval is an
# in-process asyncio task (backend/app/agent/orthestrator.py); if Cloud Run
# scales to zero mid-pause, the approval gate the demo exists to show is gone.
# One always-warm instance keeps it alive and costs ~nothing while idle.

set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to your GCP project id}"
: "${REGION:=europe-west1}"
: "${IMAGE:=${REGION}-docker.pkg.dev/${GCP_PROJECT}/taskpilot/api}"
: "${INSTANCE:=taskpilot-api}"
: "${DB_INSTANCE:=taskpilot-db}"
: "${DB_NAME:=taskpilot}"
: "${DB_USER:=taskpilot}"
: "${DB_PASS:?set DB_PASS to a strong password}"

# Cloud SQL: a managed Postgres, since Cloud Run has no writable volume for it.
if ! gcloud sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version POSTGRES_16 \
    --tier db-f1-micro \
    --region "$REGION" \
    --no-assign-ip          # private only; Cloud Run connects over the VPC
  gcloud sql databases create "$DB_NAME" --instance "$DB_INSTANCE"
fi

# Cloud SQL proxy is how Cloud Run reaches a private-instance Postgres.
gcloud services enable sqladmin.googleapis.com vpcaccess.googleapis.com

gcloud run deploy "$INSTANCE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 1 \
  --memory 4Gi \
  --cpu 2 \
  --timeout 3600 \
  --add-cloudsql-instances "${DB_INSTANCE}:${REGION}" \
  --set-env-vars \
      "TASKPILOT_DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}" \
      "TASKPILOT_MCP_ENABLED=true" \
      "TASKPILOT_ALLOW_SCRIPTED_FALLBACK=true" \
      "TASKPILOT_BROWSER_HEADLESS=true" \
  --set-secrets "TASKPILOT_LLM_API_KEY=taskpilot-llm-key:latest"

echo
echo "API URL:"
gcloud run services describe "$INSTANCE" \
  --region "$REGION" --format='value(status.url)'
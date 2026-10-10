#!/usr/bin/env bash
# Build the image with Cloud Build and deploy to Cloud Run.
# Usage:  set -a; source .env; set +a; ./deploy.sh
# Requires: gcloud authenticated, project set (or GCP_PROJECT exported), run + cloudbuild APIs enabled.
set -euo pipefail

SERVICE="${SERVICE:-csci599-a1}"
REGION="${REGION:-us-west1}"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
IMAGE="gcr.io/${PROJECT}/${SERVICE}"

for var in OPENAI_API_KEY FINNHUB_API_KEY TWELVEDATA_API_KEY FRED_API_KEY TAVILY_API_KEY; do
  [[ -n "${!var:-}" ]] || { echo "error: $var is not set (did you source .env?)" >&2; exit 1; }
done
[[ -n "$PROJECT" ]] || { echo "error: no GCP project; export GCP_PROJECT or run 'gcloud config set project'" >&2; exit 1; }

gcloud builds submit --project "$PROJECT" --tag "$IMAGE" .

gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --image "$IMAGE" \
  --platform managed \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --min-instances 0 \
  --max-instances 1 \
  --set-env-vars "OPENAI_API_KEY=${OPENAI_API_KEY},OPENAI_MODEL=${OPENAI_MODEL:-gpt-5-mini},FINNHUB_API_KEY=${FINNHUB_API_KEY},TWELVEDATA_API_KEY=${TWELVEDATA_API_KEY},FRED_API_KEY=${FRED_API_KEY},TAVILY_API_KEY=${TAVILY_API_KEY},MCP_SERVERS_CONFIG=/app/mcp_config.json,LOG_LEVEL=INFO"

gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)'

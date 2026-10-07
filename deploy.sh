#!/usr/bin/env bash
# Requires OPENAI_API_KEY, FINNHUB_API_KEY, FRED_API_KEY, TAVILY_API_KEY, GCP_PROJECT in the shell.
set -euo pipefail

SERVICE=market-watcher
REGION=us-west1
IMAGE="gcr.io/${GCP_PROJECT}/${SERVICE}"

gcloud builds submit --project "$GCP_PROJECT" --tag "$IMAGE"

gcloud run deploy "$SERVICE" \
  --project "$GCP_PROJECT" \
  --image "$IMAGE" \
  --region "$REGION" \
  --max-instances 1 \
  --allow-unauthenticated \
  --set-env-vars "OPENAI_API_KEY=${OPENAI_API_KEY},FINNHUB_API_KEY=${FINNHUB_API_KEY},FRED_API_KEY=${FRED_API_KEY},TAVILY_API_KEY=${TAVILY_API_KEY},MCP_SERVERS_CONFIG=/app/mcp_config.json"

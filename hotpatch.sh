#!/usr/bin/env bash
# hotpatch.sh — Copy changed code into running containers and restart.
# Usage: ./hotpatch.sh
# Skips full rebuild — only updates Python source files.

set -e

API_CONTAINER=$(docker compose ps -q api)
UI_CONTAINER=$(docker compose ps -q ui)
WORKER_CONTAINER=$(docker compose ps -q worker)

if [ -z "$API_CONTAINER" ] || [ -z "$UI_CONTAINER" ]; then
  echo "Error: api or ui container not running. Start with: docker compose up -d"
  exit 1
fi

echo "Patching api..."
docker cp api/. "$API_CONTAINER":/app/api/
docker cp prompts/. "$API_CONTAINER":/app/prompts/
docker compose restart api

echo "Patching ui..."
docker cp ui/. "$UI_CONTAINER":/app/ui/
docker compose restart ui

if [ -n "$WORKER_CONTAINER" ]; then
  echo "Patching worker..."
  docker cp api/. "$WORKER_CONTAINER":/app/api/
  docker cp prompts/. "$WORKER_CONTAINER":/app/prompts/
  docker compose restart worker
fi

echo "Done. Containers restarted with updated code."

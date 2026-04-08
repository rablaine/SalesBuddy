---
description: "Use when deploying the AI gateway to Azure App Service, deploying to staging or production slots, checking gateway health, rolling back a broken deploy, or building the gateway deploy zip. Trigger words: deploy gateway, deploy staging, deploy prod, gateway health, rollback, deploy zip, app-notehelper-ai."
---

# Gateway Deployment

## Deployment Rules

- **NEVER deploy to prod without verifying staging first.** Deploy to staging, hit `/health`, confirm 200, only then deploy to prod.
- **NEVER deploy to both slots simultaneously.** Staging is the canary. If staging breaks, prod is unaffected.
- **Before building a deploy zip, include ALL required files** (see manifest below). Do not guess - verify.
- **After deploying, verify with `GET /health`** (returns `{"status": "ok"}`). See HTTP status reference below.

## Deploy Zip - Required Files

All 5 files from `infra/gateway/` must be in the zip root:

1. `gateway.py` - Main Flask app
2. `sharing_hub.py` - Socket.IO sharing server
3. `openai_client.py` - Azure OpenAI client wrapper
4. `prompts.py` - AI prompt templates
5. `requirements.txt` - Python dependencies

If `gateway.py` adds new imports in the future, the new files must also be included.

## HTTP Status Cheat Sheet

| Status | Meaning |
|--------|-----------------------------------------------|
| `200`  | Healthy - app is running and responding |
| `403`  | App is running, but auth rejected the request (check gateway secret / JWT) |
| `404`  | Endpoint doesn't exist (check route definitions - NOT "service is down") |
| `502`  | Container failed to start (check App Service logs: `az webapp log tail`) |
| `503`  | App Service is restarting or overloaded |

## Rollback Procedure

If a deploy breaks a slot:

1. Check logs: `az webapp log tail -g NoteHelper_Resources -n app-notehelper-ai [-s staging]`
2. Redeploy the last known-good zip: `az webapp deploy -g NoteHelper_Resources -n app-notehelper-ai [-s staging] --src-path infra/gateway/gateway-deploy.zip --type zip --clean true`
3. Verify with `GET /health` -> 200

## Infrastructure Reference

- **App Service:** `app-notehelper-ai` in resource group `NoteHelper_Resources`
- **Staging slot:** `app-notehelper-ai-staging` (canary for deploys)
- **Runtime:** Python 3.11, Gunicorn
- **Startup command:** `gunicorn --bind=0.0.0.0:8000 --threads 4 gateway:app`

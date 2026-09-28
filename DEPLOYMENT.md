# Deployment

This repo runs as a **two-process app** (FastAPI backend + Streamlit UI), so
it deploys to **Render** as **two services** defined in
[`render.yaml`](render.yaml): `signalstack-backend` and `signalstack-frontend`.
There is no Vercel configuration: `vercel.json` has been deleted, and Vercel's
serverless runtime cannot host the Streamlit UI.

For running locally, see the **Run** section of the [README](README.md).

## Render (recommended)

Render already has `render.yaml` and a working `Dockerfile`/`start.sh`. On first deploy:

1. Go to https://dashboard.render.com/ and create a new **Web Service** from your GitHub repo `rishitha6274/signalstack`.
2. Render will detect `render.yaml`. Accept it — it defines both services.
3. Set the two secrets when prompted: `HINDSIGHT_API_KEY` and `GROQ_API_KEY` (sync: false in the blueprint). Get them from:
   - Hindsight: https://ui.hindsight.vectorize.io → Connect (API key, starts with `hsk_`)
   - Groq: https://console.groq.com/keys (starts with `gsk_`)
4. Deploy. Render creates **both** services. The backend binds `0.0.0.0:$PORT`
   and is health-checked at `/health`; the frontend is health-checked at
   `/_stcore/health`. Neither is loopback-only — they are separate services on
   separate hostnames.
5. Set `BACKEND_URL` on `signalstack-frontend` to the backend's public URL
   (e.g. `https://signalstack-backend.onrender.com`) and redeploy. The frontend
   cannot reach the API without it, because Render routes only to a service's
   own published `PORT`.
6. First cold start after idle (free tier) takes a few seconds while both processes boot.

Backend env vars (already set in the blueprint): `HINDSIGHT_BASE_URL`,
`GROQ_BASE_URL`, `AUTOSEED=1`, `ENABLE_DEMO_RESET="0"`.
Frontend env vars: `BACKEND_URL` and `API_KEY` (both `sync: false`, so you set
them). If you set `API_KEY` on the backend you **must** set the same value on
the frontend, or every write from the UI is refused with 401.

## Quick verification after Render deploy

- Open `https://signalstack-frontend.onrender.com/` → should load the Streamlit UI.
- Select Nimbus AI → Full timeline → Get Strategic Read. Should return a grounded read within ~10–30s (with possible Groq rate-limit waits).
- Select Vertex Cloud → should return a refusal (`confidence: none`).
- UI health: `https://signalstack-frontend.onrender.com/_stcore/health` returns 200.
- API health: `https://signalstack-backend.onrender.com/health` returns 200 and reports `demo_reset_enabled: false`.

## Notes

- Never commit `.env`. Secrets are injected at runtime only.
- `AUTOSEED=1` seeds demo data on first boot if memory is empty (as designed).
- CORS in `backend/main.py` allows `localhost:8501` for local dev. On Render the
  UI and API are separate services on separate origins, so the browser does hit
  CORS; the deployed API must allow the frontend's hostname.
- The single-container Docker path still exists for local use, where the UI
  reaches the API over loopback via `SIGNAL_STACK_API`. It is not what
  `render.yaml` deploys.

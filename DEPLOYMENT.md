# Vercel deployment notes

This repo runs as a **two-process app** (FastAPI backend + Streamlit UI). Vercel's serverless Python runtime can only run a single ASGI/WSGI app per function and has no long-lived process model for Streamlit's Tornado server. The `vercel.json` above deploys **only the FastAPI backend** at the project root. The Streamlit UI will **not** run on Vercel in this configuration.

If you want the full UI on Vercel, you'd need to rewrite `frontend/app.py` to a static/Next.js frontend that calls the Vercel-deployed API (or host the UI elsewhere). For this project, **Render (Docker)** is the correct host for the full Streamlit app.

## Render (recommended)

Render already has `render.yaml` and a working `Dockerfile`/`start.sh`. On first deploy:

1. Go to https://dashboard.render.com/ and create a new **Web Service** from your GitHub repo `rishitha6274/signalstack`.
2. Render will detect `render.yaml` (Docker runtime). Accept it.
3. Set the two secrets when prompted: `HINDSIGHT_API_KEY` and `GROQ_API_KEY` (sync: false in the blueprint). Get them from:
   - Hindsight: https://ui.hindsight.vectorize.io → Connect (API key, starts with `hsk_`)
   - Groq: https://console.groq.com/keys (starts with `gsk_`)
4. Deploy. The service exposes only the Streamlit UI on port 8501; the API stays loopback-only inside the container. Health check is `/_stcore/health`.
5. First cold start after idle (free tier) takes a few seconds while both processes boot.

Env vars (already set): `HINDSIGHT_BASE_URL`, `GROQ_BASE_URL`, `AUTOSEED=1`, `SIGNAL_STACK_API=http://127.0.0.1:8000`, `PORT=8501`.

## Vercel (API-only)

If you still want to deploy to Vercel:

1. Import `rishitha6274/signalstack` into Vercel (https://vercel.com/new).
2. Framework: Other. Build command: (leave empty) — `@vercel/python` builds from `requirements.txt`.
3. Add Environment Variables in Project Settings → Environment Variables:
   - `HINDSIGHT_API_KEY` (Production/Preview/Development)
   - `GROQ_API_KEY`
   - `HINDSIGHT_BASE_URL` = `https://api.hindsight.vectorize.io`
   - `GROQ_BASE_URL` = `https://api.groq.com/openai/v1`
   - `AUTOSEED` = `1` (optional; seeding on cold starts is fine for demos)
4. Deploy. The root will serve the FastAPI app (`backend/main.py`). Interactive docs at `https://<project>.vercel.app/docs`. The Streamlit UI will **not** be available there.

**Important:** Vercel serverless has a request timeout (10s on Hobby, up to 60s on Pro) and no background processes; long synthesis calls that wait on Groq rate limits may hit timeouts. Render's Docker container runs continuously (while awake) and is better suited for this demo.

## Quick verification after Render deploy

- Open `https://<your-service>.onrender.com/` → should load the Streamlit UI.
- Select Nimbus AI → Full timeline → Get Strategic Read. Should return a grounded read within ~10–30s (with possible Groq rate-limit waits).
- Select Vertex Cloud → should return a refusal (`confidence: none`).
- Health: `https://<your-service>.onrender.com/_stcore/health` returns 200.

## Notes

- Never commit `.env`. Secrets are injected at runtime only.
- `AUTOSEED=1` seeds demo data on first boot if memory is empty (as designed).
- CORS in `backend/main.py` allows `localhost:8501` for local dev; on Render the UI and API share the same container over loopback so CORS isn't hit by external browsers.

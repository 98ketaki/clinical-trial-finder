# Running the app locally

Two processes: the FastAPI backend (`:8000`) and the Next.js frontend (`:3000`).
Run them in separate terminals. All commands are from the repo root unless noted.

## Prerequisites

- Python deps installed in `.venv` (already done): `.venv/bin/pip install -r requirements.txt`
- Node deps installed in `frontend/` (already done): `cd frontend && npm install`
- `.env` filled in with `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE`, and a **working** `DATABASE_URL` (see below).

## 0. DATABASE_URL — the thing that bites you

The matching layer (embed, vector search, the API) needs a **direct Postgres**
connection for pgvector. The Supabase REST API cannot run `<=>` cosine search or DDL.

- The direct host `db.<ref>.supabase.co` is **IPv6-only**. On a network without IPv6
  egress it times out. Use the **IPv4 connection pooler** instead.
- Get the exact string from **Supabase Dashboard → Settings → Database →
  Connection string → "Session pooler"**. Do not hand-edit the region.
- Format:
  ```
  postgresql://postgres.<project-ref>:<DB-PASSWORD>@aws-1-<region>.pooler.supabase.com:5432/postgres
  ```
  - Username is `postgres.<project-ref>` (e.g. `postgres.havswqhcruipsuootnth`).
  - Host prefix may be `aws-0-` or `aws-1-` — copy whatever the dashboard shows
    (this project is `aws-1-us-west-2`).
  - `<DB-PASSWORD>` is the **Database password** (Settings → Database → reset it if
    unknown), NOT the service-role key or anon key.

Quick connectivity check (prints the connected user, no secrets):
```bash
.venv/bin/python -c "import os,psycopg2;from dotenv import load_dotenv;load_dotenv();c=psycopg2.connect(os.getenv('DATABASE_URL'),connect_timeout=8);cur=c.cursor();cur.execute('select current_user');print('connected as',cur.fetchone()[0])"
```

## 1. Apply the database migration

Creates the pgvector extension + `trial_embeddings`, `patient_sessions`, `feedback`.
Non-destructive (`IF NOT EXISTS` throughout).

Either paste [supabase/schema_with_rls.sql](supabase/schema_with_rls.sql) into the
Supabase **SQL Editor** (runs server-side, no local network needed), or run:
```bash
.venv/bin/python -c "import os,psycopg2;from dotenv import load_dotenv;load_dotenv();c=psycopg2.connect(os.getenv('DATABASE_URL'));c.autocommit=True;c.cursor().execute(open('supabase/schema_with_rls.sql').read());print('migration applied')"
```

## 2. Build the data (pipeline)

The pipeline has three stages. ingest + parse already populated `trials` and
`eligibility_criteria`. Embeddings are needed for search:

```bash
# Smoke test against the trials already parsed:
.venv/bin/python -m backend.sync.embed --limit 9

# Full backfill (only when ready — ~2258 OpenAI calls each):
.venv/bin/python -m backend.sync.ingest        # refresh trials from ClinicalTrials.gov
.venv/bin/python -m backend.sync.parse         # raw_eligibility -> eligibility_criteria (GPT-4o-mini)
.venv/bin/python -m backend.sync.embed         # eligibility -> trial_embeddings (ada-002)
```
All three are idempotent (content-hash gated) and support `--dry-run` / `--limit`.

## 3. Run the backend API

```bash
.venv/bin/uvicorn backend.main:app --reload --port 8000
```
Open <http://localhost:8000/docs>. Try `POST /match`:
```json
{ "stage": "IV", "ecog": 1, "age": 60,
  "biomarkers": [{ "name": "EGFR", "status": "positive" }] }
```
Endpoints: `POST /match`, `POST /feedback`, `GET /trial/{nct_id}`, `GET /health`.

## 4. Run the frontend

```bash
cd frontend
cp .env.local.example .env.local     # NEXT_PUBLIC_API_URL=http://localhost:8000
npm run dev
```
Open <http://localhost:3000>, fill the form, see ranked results with explanations
and thumbs up/down.

## Notes

- With only 9 trials embedded, matches are a wiring smoke test, not real ranking.
  Real results need the full backfill in step 2.
- Patient health data lives only in `patient_sessions` and expires after 2 hours;
  expired rows are purged on each `/match` call.
- CORS allows `http://localhost:3000` by default. Override with `CORS_ORIGINS`
  (comma-separated) in the environment if the frontend runs elsewhere.

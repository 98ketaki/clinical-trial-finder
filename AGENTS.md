Clinical Trial Finder 

#### What This Is
A full-stack web app that matches lung cancer patients to relevant clinical trials from ClinicalTrials.gov. Patients enter their clinical profile (age, stage, biomarkers, ECOG, prior treatments, location) and receive a ranked list of trials with plain-language explanations. This project is  focused on LLM pipeline design and full-stack deployment.

#### Stack

Backend: FastAPI (Python)
Database: Supabase (Postgres + pgvector)
LLM - batch parsing: OpenAI GPT-4o-mini with response_format={"type": "json_object"}
LLM - patient explanation: Anthropic Claude
Embeddings: OpenAI text-embedding-ada-002 (1536 dimensions)
Frontend: Next.js 
Infrastructure: Docker, GCP Cloud Run, GCP Cloud Scheduler
Sync: Weekly every Sunday via cron

#### Project Structure
clinical-trial-matcher/
├── backend/
│   ├── main.py              # FastAPI app
│   ├── sync/
│   │   ├── ingest.py        # ClinicalTrials.gov ingestion
│   │   ├── parse.py         # LLM eligibility criteria parser
│   │   └── embed.py         # Embedding generation
│   ├── matching/
│   │   ├── filters.py       # Hard filters (age, sex, ECOG, location)
│   │   └── search.py        # pgvector semantic search
│   ├── explain.py           # Claude explanation generation
│   └── models.py            # Pydantic schemas
├── frontend/                # Next.js app
├── supabase/
│   └── schema.sql           # DB schema
├── tests/
│   └── test_cases.json      # Patient profiles + expected matches for eval
├── .env                     # API keys (never commit)
└── docker-compose.yml


#### Database Schema (Supabase)

trials — structured fields from CT.gov (nct_id, title, status, phase, conditions, min_age, max_age, sex, locations JSONB, raw_eligibility TEXT, content_hash, last_synced_at)
eligibility_criteria — LLM-parsed output, one row per criterion (trial_id, cancer_type, stages[], histology[], biomarkers_required JSONB, biomarkers_excluded JSONB, prior_treatments_required[], prior_treatments_excluded[], ecog_max INT, notes TEXT)
trial_embeddings — pgvector (trial_id, embedding vector(1536), content TEXT)
patient_sessions — expires 24hrs (id UUID, cancer_type, stage, histology, biomarkers JSONB, prior_treatments[], ecog INT, age INT, sex, location, created_at, expires_at)
feedback — (trial_id, session_id, rating: thumbs_up | thumbs_down, created_at)

#### API Endpoints

POST /match — takes patient session, returns ranked trials with explanations
POST /feedback — stores thumbs up/down rating
GET /trial/{nct_id} — returns trial details

ClinicalTrials.gov API

Base URL: https://clinicaltrials.gov/api/v2/studies
Key params: query.cond="lung cancer", filter.overallStatus="RECRUITING", pageSize=100
Date filter for weekly sync: query.term=AREA[LastUpdatePostDate]RANGE[<date>,MAX]
Response path to eligibility: protocolSection.eligibilityModule.eligibilityCriteria
Response path to locations: protocolSection.contactsLocationsModule.locations
Location fields always present: facility, city, country, geoPoint. state is 71% populated — do NOT use for filtering.

#### LLM Parsing Schema
json{
  "cancer_type": "string",
  "stages": ["I|II|III|IV"],
  "histology": ["string"],
  "histology_excluded": ["string"],
  "biomarkers_required": [{"name": "canonical_name", "status": "positive|negative|mutated|wild-type|any|<variant>"}],
  "biomarkers_excluded": [{"name": "canonical_name", "status": "string"}],
  "prior_treatments_required": ["string"],
  "prior_treatments_excluded": ["string"],
  "ecog_max": "int|null",
  "notes": "string"
}
Stage synonyms: "metastatic"/"extensive stage" → IV; "locally advanced" → III; "limited stage" → I–III; "unresectable" → III–IV; "early stage" → I–II.
Histology is never a biomarker — always goes in histology/histology_excluded fields.
Eligibility blobs contain markdown escapes (>=, <=, ^) — strip before parsing.
Canonical Biomarker List
EGFR, EGFR_T790M, EGFR_C797S, EGFR_exon20ins, ALK, ROS1, KRAS, KRAS_G12C, HER2, PD_L1, BRAF, BRAF_V600E, MET, MET_exon14, SSTR, RET, NTRK
Matching Logic
Stage 1 hard filters: status=RECRUITING, age in range, sex match, ECOG ≤ ecog_max, location city/country match, unknown biomarkers excluded.
Stage 2 semantic: embed patient profile as natural language, cosine similarity against trial_embeddings, return top 10.

#### Key Constraints
Patient session data expires after 2 hours — never log or persist health data beyond this
Always include "not medical advice" disclaimer in any patient-facing output
When fewer than 3 trials match: show matches found + "Only these matches were found — do you want to change any input and try again?"
Data may be up to 7 days stale — surface this in the UI


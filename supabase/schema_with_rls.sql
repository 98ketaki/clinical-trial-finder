-- Supabase / Postgres schema migration for `trials` table with RLS and policies

-- This file is non-destructive. It preserves existing `trials` data and only
-- creates missing objects, indexes, and policies.

CREATE TABLE IF NOT EXISTS trials (
  nct_id TEXT PRIMARY KEY,
  title TEXT,
  overall_status TEXT,
  phase TEXT,
  conditions TEXT[],
  interventions TEXT[],
  sex TEXT,
  min_age INT,
  max_age INT,
  locations JSONB,
  raw_eligibility TEXT,
  content_hash TEXT,
  last_updated DATE,
  last_synced_at TIMESTAMPTZ DEFAULT now(),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE trials
  ADD COLUMN IF NOT EXISTS title TEXT,
  ADD COLUMN IF NOT EXISTS overall_status TEXT,
  ADD COLUMN IF NOT EXISTS phase TEXT,
  ADD COLUMN IF NOT EXISTS conditions TEXT[],
  ADD COLUMN IF NOT EXISTS interventions TEXT[],
  ADD COLUMN IF NOT EXISTS sex TEXT,
  ADD COLUMN IF NOT EXISTS min_age INT,
  ADD COLUMN IF NOT EXISTS max_age INT,
  ADD COLUMN IF NOT EXISTS locations JSONB,
  ADD COLUMN IF NOT EXISTS raw_eligibility TEXT,
  ADD COLUMN IF NOT EXISTS content_hash TEXT,
  ADD COLUMN IF NOT EXISTS last_updated DATE,
  ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Trigger to keep `updated_at` current
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_set_updated_at'
      AND tgrelid = 'trials'::regclass
  ) THEN
    CREATE TRIGGER trg_set_updated_at
    BEFORE UPDATE ON trials
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END;
$$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trials_content_hash ON trials (content_hash);
CREATE INDEX IF NOT EXISTS idx_trials_min_age ON trials (min_age);
CREATE INDEX IF NOT EXISTS idx_trials_max_age ON trials (max_age);
CREATE INDEX IF NOT EXISTS idx_trials_overall_status ON trials (overall_status);

-- GIN indexes for array/JSONB fields for faster searching
CREATE INDEX IF NOT EXISTS idx_trials_conditions_gin ON trials USING GIN (conditions);
CREATE INDEX IF NOT EXISTS idx_trials_interventions_gin ON trials USING GIN (interventions);
CREATE INDEX IF NOT EXISTS idx_trials_locations_gin ON trials USING GIN (locations);

-- SECURITY: Enable Row Level Security (RLS) and create policies.
-- Ingestion should use the Supabase service_role key (server-side) which bypasses RLS.
-- This policy restricts SELECT to authenticated users only.

ALTER TABLE trials ENABLE ROW LEVEL SECURITY;

-- Revoke broad access and rely on policies
REVOKE ALL ON TABLE trials FROM PUBLIC;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'select_for_authenticated'
      AND schemaname = current_schema()
      AND tablename = 'trials'
  ) THEN
    CREATE POLICY select_for_authenticated ON trials
      FOR SELECT USING (auth.role() = 'authenticated');
  END IF;
END;
$$;

-- Note: No INSERT/PATCH policies are created because ingestion should use
-- the service_role key server-side (it bypasses RLS). If you need to allow
-- other roles to write, add explicit policies that check auth.role() or auth.uid().


-- LLM-parsed eligibility criteria, one row per trial. Non-destructive.

CREATE TABLE IF NOT EXISTS eligibility_criteria (
  trial_id                  TEXT PRIMARY KEY REFERENCES trials(nct_id) ON DELETE CASCADE,
  cancer_type               TEXT,
  stages                    TEXT[],
  histology                 TEXT[],
  histology_excluded        TEXT[],
  biomarkers_required       JSONB,
  biomarkers_excluded       JSONB,
  prior_treatments_required TEXT[],
  prior_treatments_excluded TEXT[],
  ecog_max                  INT,
  notes                     TEXT,
  content_hash              TEXT,
  parse_status              TEXT,
  parsed_at                 TIMESTAMPTZ DEFAULT now(),
  created_at                TIMESTAMPTZ DEFAULT now(),
  updated_at                TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE eligibility_criteria
  ADD COLUMN IF NOT EXISTS cancer_type TEXT,
  ADD COLUMN IF NOT EXISTS stages TEXT[],
  ADD COLUMN IF NOT EXISTS histology TEXT[],
  ADD COLUMN IF NOT EXISTS histology_excluded TEXT[],
  ADD COLUMN IF NOT EXISTS biomarkers_required JSONB,
  ADD COLUMN IF NOT EXISTS biomarkers_excluded JSONB,
  ADD COLUMN IF NOT EXISTS prior_treatments_required TEXT[],
  ADD COLUMN IF NOT EXISTS prior_treatments_excluded TEXT[],
  ADD COLUMN IF NOT EXISTS ecog_max INT,
  ADD COLUMN IF NOT EXISTS notes TEXT,
  ADD COLUMN IF NOT EXISTS content_hash TEXT,
  ADD COLUMN IF NOT EXISTS parse_status TEXT,
  ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Reuse the shared set_updated_at() trigger function defined above.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_eligibility_set_updated_at'
      AND tgrelid = 'eligibility_criteria'::regclass
  ) THEN
    CREATE TRIGGER trg_eligibility_set_updated_at
    BEFORE UPDATE ON eligibility_criteria
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_eligibility_content_hash ON eligibility_criteria (content_hash);
CREATE INDEX IF NOT EXISTS idx_eligibility_parse_status ON eligibility_criteria (parse_status);

-- SECURITY: same posture as `trials`. Ingestion/parsing uses the service_role
-- key (bypasses RLS); SELECT is restricted to authenticated users.

ALTER TABLE eligibility_criteria ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE eligibility_criteria FROM PUBLIC;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE policyname = 'select_for_authenticated'
      AND schemaname = current_schema()
      AND tablename = 'eligibility_criteria'
  ) THEN
    CREATE POLICY select_for_authenticated ON eligibility_criteria
      FOR SELECT USING (auth.role() = 'authenticated');
  END IF;
END;
$$;


-- pgvector extension for semantic search.
CREATE EXTENSION IF NOT EXISTS vector;

-- Per-trial embedding (OpenAI ada-002, 1536-dim). Non-destructive.
CREATE TABLE IF NOT EXISTS trial_embeddings (
  trial_id     TEXT PRIMARY KEY REFERENCES trials(nct_id) ON DELETE CASCADE,
  embedding    vector(1536),
  content      TEXT,
  content_hash TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'trg_trial_embeddings_set_updated_at'
      AND tgrelid = 'trial_embeddings'::regclass
  ) THEN
    CREATE TRIGGER trg_trial_embeddings_set_updated_at
    BEFORE UPDATE ON trial_embeddings
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_trial_embeddings_cosine ON trial_embeddings
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


-- Patient intake sessions. Holds health data transiently; expires after 2 hours.
CREATE TABLE IF NOT EXISTS patient_sessions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cancer_type      TEXT,
  stage            TEXT,
  histology        TEXT,
  biomarkers       JSONB,
  prior_treatments TEXT[],
  ecog             INT,
  age              INT,
  sex              TEXT,
  location         TEXT,
  created_at       TIMESTAMPTZ DEFAULT now(),
  expires_at       TIMESTAMPTZ DEFAULT now() + interval '2 hours'
);

CREATE INDEX IF NOT EXISTS idx_patient_sessions_expires_at ON patient_sessions (expires_at);

CREATE TABLE IF NOT EXISTS feedback (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trial_id   TEXT REFERENCES trials(nct_id) ON DELETE CASCADE,
  session_id UUID REFERENCES patient_sessions(id) ON DELETE CASCADE,
  rating     TEXT CHECK (rating IN ('thumbs_up', 'thumbs_down')),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feedback_trial_id ON feedback (trial_id);
CREATE INDEX IF NOT EXISTS idx_feedback_session_id ON feedback (session_id);

-- SECURITY: backend uses the service_role key (bypasses RLS). Enable RLS so these
-- tables aren't world-readable through the public/anon API. The backend never
-- exposes the DB directly; the Next.js client talks only to the FastAPI service.
ALTER TABLE trial_embeddings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_sessions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback          ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE trial_embeddings FROM PUBLIC;
REVOKE ALL ON TABLE patient_sessions FROM PUBLIC;
REVOKE ALL ON TABLE feedback         FROM PUBLIC;
-- No public policies: only the service_role (which bypasses RLS) may read/write.

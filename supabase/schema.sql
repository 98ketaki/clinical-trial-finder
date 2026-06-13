-- Supabase / Postgres schema for `trials` table

DROP TABLE IF EXISTS trials;

CREATE TABLE trials (
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

-- Trigger to keep `updated_at` current
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON trials
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- Indexes
CREATE INDEX idx_trials_content_hash ON trials (content_hash);
CREATE INDEX idx_trials_min_age ON trials (min_age);
CREATE INDEX idx_trials_max_age ON trials (max_age);
CREATE INDEX idx_trials_overall_status ON trials (overall_status);

-- GIN indexes for array/JSONB fields for faster searching
CREATE INDEX idx_trials_conditions_gin ON trials USING GIN (conditions);
CREATE INDEX idx_trials_interventions_gin ON trials USING GIN (interventions);
CREATE INDEX idx_trials_locations_gin ON trials USING GIN (locations);


-- LLM-parsed eligibility criteria, one row per trial.
DROP TABLE IF EXISTS eligibility_criteria;

CREATE TABLE eligibility_criteria (
  trial_id                  TEXT PRIMARY KEY REFERENCES trials(nct_id) ON DELETE CASCADE,
  cancer_type               TEXT,
  stages                    TEXT[],
  histology                 TEXT[],
  histology_excluded        TEXT[],
  biomarkers_required       JSONB,   -- [{"name": "...", "status": "..."}]
  biomarkers_excluded       JSONB,   -- [{"name": "...", "status": "..."}]
  prior_treatments_required TEXT[],
  prior_treatments_excluded TEXT[],
  ecog_max                  INT,
  notes                     TEXT,
  content_hash              TEXT,        -- the trials.content_hash this row was parsed from
  parse_status              TEXT,        -- 'parsed' | 'failed'
  parsed_at                 TIMESTAMPTZ DEFAULT now(),
  created_at                TIMESTAMPTZ DEFAULT now(),
  updated_at                TIMESTAMPTZ DEFAULT now()
);

-- Reuse the shared set_updated_at() trigger function defined above.
CREATE TRIGGER trg_eligibility_set_updated_at
BEFORE UPDATE ON eligibility_criteria
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_eligibility_content_hash ON eligibility_criteria (content_hash);
CREATE INDEX idx_eligibility_parse_status ON eligibility_criteria (parse_status);


-- pgvector extension for semantic search.
CREATE EXTENSION IF NOT EXISTS vector;

-- Per-trial embedding of the structured eligibility text (OpenAI ada-002, 1536-dim).
DROP TABLE IF EXISTS trial_embeddings;

CREATE TABLE trial_embeddings (
  trial_id     TEXT PRIMARY KEY REFERENCES trials(nct_id) ON DELETE CASCADE,
  embedding    vector(1536),
  content      TEXT,           -- the natural-language text that was embedded
  content_hash TEXT,           -- the trials.content_hash this embedding was built from
  created_at   TIMESTAMPTZ DEFAULT now(),
  updated_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TRIGGER trg_trial_embeddings_set_updated_at
BEFORE UPDATE ON trial_embeddings
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

-- ivfflat index for cosine similarity search.
CREATE INDEX idx_trial_embeddings_cosine ON trial_embeddings
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


-- Patient intake sessions. Holds health data transiently; expires after 2 hours.
DROP TABLE IF EXISTS feedback;
DROP TABLE IF EXISTS patient_sessions;

CREATE TABLE patient_sessions (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cancer_type      TEXT,
  stage            TEXT,
  histology        TEXT,
  biomarkers       JSONB,          -- [{"name": "...", "status": "..."}]
  prior_treatments TEXT[],
  ecog             INT,
  age              INT,
  sex              TEXT,
  location         TEXT,
  created_at       TIMESTAMPTZ DEFAULT now(),
  expires_at       TIMESTAMPTZ DEFAULT now() + interval '2 hours'
);

CREATE INDEX idx_patient_sessions_expires_at ON patient_sessions (expires_at);


-- Thumbs up/down feedback on a trial within a session.
CREATE TABLE feedback (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trial_id   TEXT REFERENCES trials(nct_id) ON DELETE CASCADE,
  session_id UUID REFERENCES patient_sessions(id) ON DELETE CASCADE,
  rating     TEXT CHECK (rating IN ('thumbs_up', 'thumbs_down')),
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_feedback_trial_id ON feedback (trial_id);
CREATE INDEX idx_feedback_session_id ON feedback (session_id);

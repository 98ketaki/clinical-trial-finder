## Plan: ClinicalTrials.gov ingestion script

TL;DR - Implement a sync `backend/sync/ingest.py` that paginates the CT.gov v2 API, flattens required fields, computes an MD5 `content_hash` of `raw_eligibility`, and upserts into Supabase `trials` table. Use the sync `supabase-py` client and `httpx` (sync client) per alignment. Support an optional `--since_date` (YYYY-MM-DD) for weekly syncs. Handle rate limits with exponential backoff and log added/updated/skipped counts.

**Steps**
1. Scaffolding
   - Create directories: `backend/`, `backend/sync/` and `backend/sync/__init__.py`.
   - Add `requirements.txt` with minimal deps: `supabase-py`, `httpx`, `python-dotenv`, `tenacity`, `tqdm` (optional).
2. Implement `backend/sync/ingest.py`
   - CLI: parse `--since_date` (format `YYYY-MM-DD`) and `--dry-run` flag.
   - HTTP client: use `httpx` sync client to call `https://clinicaltrials.gov/api/v2/studies` with params:
     - `query.cond=lung cancer`
     - `filter.overallStatus=RECRUITING`
     - `pageSize=100`
     - if `since_date` set add `query.term=AREA[LastUpdatePostDate]RANGE[<since_date>,MAX]`
   - Pagination: follow `nextPageToken` until none; fetch each page.
   - For each study record:
     - Extract/flatten into dict: `nct_id`, `title`, `overall_status`, `phase`, `conditions` (list), `interventions` (list of names), `sex`, `min_age` (int), `max_age` (int), `locations` (raw JSONB), `raw_eligibility` (text blob), `last_updated` (date string)
     - Normalization rules:
       - Age: strip non-digits and the word "Years"; if missing or non-year units store `NULL`.
       - `interventions`: extract human-readable names where present.
     - Compute `content_hash` = MD5 of `raw_eligibility` (UTF-8)
   - Upsert into Supabase `trials` table using `supabase-py` sync client:
     - Query `trials` by `nct_id` to get existing `content_hash` (index lookup)
     - If not found: insert new row (count +1 added)
     - If found and `content_hash` differs: update fields (count +1 updated)
     - If found and `content_hash` same: skip (count +1 skipped)
   - Logging: accumulate and log counts for added, updated, skipped at end.
   - Error handling & rate limits:
     - Use `tenacity` or a custom exponential-backoff for HTTP 429, 5xx, and network errors.
     - Respect `Retry-After` header when present.
   - `__main__` block: allow direct run for bulk initial load and accept `--since_date` for weekly sync.
3. Tests & verification
   - Add unit tests that mock `httpx` responses and `supabase` client to verify parsing, content_hash logic, and upsert decision tree.
   - Manual run: `python backend/sync/ingest.py` and `python backend/sync/ingest.py --since_date 2026-05-28 --dry-run`
4. Deployment notes
   - Cloud Scheduler job runs weekly with `--since_date` set to last sync date.
   - Ensure `SUPABASE_URL` and `SUPABASE_KEY` are supplied via env or `.env`.

**Relevant files**
- `AGENTS.md` — architecture + DB schema reference
- `backend/sync/ingest.py` — new script to implement
- `requirements.txt` — new dependency manifest

**Verification**
1. Unit tests: mock CT.gov paginated responses and verify added/updated/skipped counts.
2. Integration dry-run: run against real CT.gov API with `--dry-run` to see intended DB operations.
3. DB validation: ensure `trials` table has new rows and `content_hash` set correctly.

**Decisions / Assumptions**
- Use sync `supabase-py` client (user choice) and `httpx` sync client to keep implementation simple and consistent.
- `--since_date` format: `YYYY-MM-DD` (user choice).
- `pageSize=100` per request; will paginate with `nextPageToken` until exhausted.
- Age values assumed usually in Years — non-year units will result in NULL for min/max age.

**Further Considerations**
1. Schema alignment: confirm `trials` table columns match the fields to upsert (esp. `locations` type JSONB and `last_synced_at`).
2. Large text blobs: ensure `raw_eligibility` length is acceptable for DB column type (TEXT).
3. If performance matters, consider switching to async `supabase-py` or batching DB writes using `upsert` with conflict keys.

---

If this plan looks good I will scaffold the `backend/sync/` tree and create `backend/sync/ingest.py` (implementation) next. If you want the script to use async DB calls instead, tell me and I will switch the plan to async end-to-end.
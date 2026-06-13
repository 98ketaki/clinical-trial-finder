"""LLM eligibility-criteria parser.

Stage 2 of the pipeline. Reads `raw_eligibility` blobs ingested into the `trials`
table (see ingest.py), sends each through GPT-4o-mini, and writes structured
criteria into the `eligibility_criteria` table. Re-parses only trials that are new
or whose `content_hash` changed, so it is safe to run on the weekly sync.

DB access mirrors ingest.py: direct Postgres via DATABASE_URL, or the Supabase
REST API via SUPABASE_SERVICE_ROLE.
"""

import os
import argparse
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_exception,
)
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ctgov_parse")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OPENAI_MODEL = "gpt-4o-mini"
TRIALS_TABLE = "trials"
CRITERIA_TABLE = "eligibility_criteria"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Canonical biomarker names. The LLM must map any biomarker it finds to one of these.
CANONICAL_BIOMARKERS = [
    "EGFR", "EGFR_T790M", "EGFR_C797S", "EGFR_exon20ins", "ALK", "ROS1",
    "KRAS", "KRAS_G12C", "HER2", "PD_L1", "BRAF", "BRAF_V600E", "MET",
    "MET_exon14", "SSTR", "RET", "NTRK",
]

# Stage synonym expansion, applied as a Python safety net on top of the prompt
# instruction. Keys are matched case-insensitively against any stage string the
# LLM returns; matches are expanded to the canonical Roman-numeral stages.
STAGE_SYNONYMS = {
    "metastatic": ["IV"],
    "extensive stage": ["IV"],
    "extensive-stage": ["IV"],
    "locally advanced": ["III"],
    "limited stage": ["I", "II", "III"],
    "limited-stage": ["I", "II", "III"],
    "unresectable": ["III", "IV"],
}

VALID_STAGES = ["I", "II", "III", "IV"]

SYSTEM_PROMPT = f"""You extract structured clinical-trial eligibility criteria for lung cancer trials.

Read the eligibility-criteria text and return a SINGLE JSON object with EXACTLY these keys:
{{
  "cancer_type": "string",
  "stages": ["I" | "II" | "III" | "IV"],
  "histology": ["string"],
  "histology_excluded": ["string"],
  "biomarkers_required": [{{"name": "canonical_name", "status": "positive|negative|mutated|wild-type|any|<variant>"}}],
  "biomarkers_excluded": [{{"name": "canonical_name", "status": "string"}}],
  "prior_treatments_required": ["string"],
  "prior_treatments_excluded": ["string"],
  "ecog_max": int or null,
  "notes": "string"
}}

Rules:
- Output ONLY the JSON object. No prose, no markdown fences.
- Use empty arrays [] when a list field has no values. Use null (not a string) for ecog_max when no ECOG limit is stated.
- stages: use only the Roman numerals I, II, III, IV. Apply these synonyms:
  "metastatic"/"extensive stage" -> IV; "locally advanced" -> III; "limited stage" -> I, II, III; "unresectable" -> III, IV.
- biomarkers: map every biomarker to one of these canonical names ONLY:
  {", ".join(CANONICAL_BIOMARKERS)}.
  If a biomarker is not in this list, omit it. Each biomarker is an object with "name" and "status".
- HISTOLOGY IS NEVER A BIOMARKER. Tumor histology (e.g. adenocarcinoma, squamous, small cell, non-small cell)
  goes in histology / histology_excluded, never in biomarkers.
- ecog_max: the highest ECOG performance-status value allowed (e.g. "ECOG 0-2" -> 2).
- notes: anything important that does not fit the structured fields.
"""


def supabase_headers() -> Dict[str, str]:
    key = SUPABASE_SERVICE_ROLE or SUPABASE_KEY
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def supabase_endpoint(table: str) -> str:
    return f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"


def get_pg_connection():
    try:
        import psycopg2
        from psycopg2.extras import Json
    except ImportError as e:
        raise RuntimeError(
            "psycopg2 is required for direct DATABASE_URL access. Install it with `pip install psycopg2-binary`."
        ) from e

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured for direct Postgres access.")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn, Json


def get_openai_client():
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai is required. Install it with `pip install openai`.") from e
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured in the environment.")
    return OpenAI(api_key=OPENAI_API_KEY)


def strip_markdown_escapes(text: str) -> str:
    """Eligibility blobs contain markdown escapes (\\>=, \\<=, \\^). Strip them."""
    if not text:
        return text
    text = text.replace("\\>=", ">=").replace("\\<=", "<=")
    text = text.replace("\\>", ">").replace("\\<", "<")
    text = text.replace("\\^", "^")
    return text


def build_messages(raw: str) -> List[Dict[str, str]]:
    cleaned = strip_markdown_escapes(raw)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": cleaned},
    ]


def _is_retryable_openai_error(exc: BaseException) -> bool:
    try:
        import openai
    except ImportError:
        return False
    retryable = (
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
    )
    return isinstance(exc, retryable)


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    retry=retry_if_exception(_is_retryable_openai_error),
    reraise=True,
)
def _call_openai(client: Any, raw: str) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=build_messages(raw),
        response_format={"type": "json_object"},
        temperature=0,
    )
    return resp.choices[0].message.content


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    retry=retry_if_exception_type(httpx.TransportError),
)
def supabase_request(client: httpx.Client, method: str, *args: Any, **kwargs: Any) -> httpx.Response:
    return getattr(client, method)(*args, **kwargs)


def parse_eligibility(client: Any, raw: str) -> Dict[str, Any]:
    """Call GPT-4o-mini and return the parsed JSON dict. Raises on hard failure."""
    content = _call_openai(client, raw)
    return json.loads(content)


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_biomarker_list(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(value, list):
        return out
    canonical = {b.lower(): b for b in CANONICAL_BIOMARKERS}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        # Keep only canonical biomarker names (defensive against LLM drift).
        canon = canonical.get(str(name).strip().lower())
        if not canon:
            continue
        status = item.get("status")
        out.append({"name": canon, "status": str(status).strip() if status else "any"})
    return out


def _expand_stages(value: Any) -> List[str]:
    raw_stages = _as_str_list(value)
    expanded: List[str] = []
    for s in raw_stages:
        key = s.strip().lower()
        if key in STAGE_SYNONYMS:
            expanded.extend(STAGE_SYNONYMS[key])
            continue
        # Normalize Roman numerals (strip stage prefixes like "Stage IIIA" -> "III").
        # No trailing word boundary so "IIIA"/"IVB" still resolve to the base stage.
        m = re.search(r"\b(IV|III|II|I)", s.upper())
        if m and m.group(1) in VALID_STAGES:
            expanded.append(m.group(1))
        elif s in VALID_STAGES:
            expanded.append(s)
    # Dedupe, preserve canonical order.
    return [st for st in VALID_STAGES if st in expanded]


def _as_ecog(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            return int(m.group(0))
    if isinstance(value, float):
        return int(value)
    return None


def normalize_result(data: Dict[str, Any], raw: str) -> Tuple[Dict[str, Any], str]:
    """Coerce LLM output to the exact schema. Returns (record, parse_status)."""
    record = {
        "cancer_type": (str(data.get("cancer_type")).strip() if data.get("cancer_type") else None),
        "stages": _expand_stages(data.get("stages")),
        "histology": _as_str_list(data.get("histology")),
        "histology_excluded": _as_str_list(data.get("histology_excluded")),
        "biomarkers_required": _as_biomarker_list(data.get("biomarkers_required")),
        "biomarkers_excluded": _as_biomarker_list(data.get("biomarkers_excluded")),
        "prior_treatments_required": _as_str_list(data.get("prior_treatments_required")),
        "prior_treatments_excluded": _as_str_list(data.get("prior_treatments_excluded")),
        "ecog_max": _as_ecog(data.get("ecog_max")),
        "notes": (str(data.get("notes")).strip() if data.get("notes") else None),
    }
    return record, "parsed"


def empty_result(raw: str) -> Tuple[Dict[str, Any], str]:
    """Fallback on malformed JSON / API failure. Store raw text in notes, never skip."""
    return {
        "cancer_type": None,
        "stages": [],
        "histology": [],
        "histology_excluded": [],
        "biomarkers_required": [],
        "biomarkers_excluded": [],
        "prior_treatments_required": [],
        "prior_treatments_excluded": [],
        "ecog_max": None,
        "notes": raw,
    }, "failed"


# --- Candidate selection -----------------------------------------------------

def fetch_candidates_direct(pg_conn: Any, limit: Optional[int]) -> List[Dict[str, Any]]:
    sql = (
        "SELECT t.nct_id, t.raw_eligibility, t.content_hash "
        "FROM trials t "
        "LEFT JOIN eligibility_criteria e ON e.trial_id = t.nct_id "
        "WHERE e.trial_id IS NULL OR e.content_hash IS DISTINCT FROM t.content_hash "
        "ORDER BY t.nct_id"
    )
    params: Tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    with pg_conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {"nct_id": r[0], "raw_eligibility": r[1], "content_hash": r[2]}
        for r in rows
    ]


REST_PAGE_SIZE = 1000  # PostgREST caps a single response at 1000 rows; page through.


def _rest_get_all(db_client: httpx.Client, table: str, select: str) -> List[Dict[str, Any]]:
    """Fetch every row from a table via PostgREST, paging with Range headers.

    A plain select is capped at 1000 rows by PostgREST, so we must paginate or we
    silently miss everything past the first page.
    """
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        headers = {
            **supabase_headers(),
            "Range-Unit": "items",
            "Range": f"{offset}-{offset + REST_PAGE_SIZE - 1}",
        }
        res = supabase_request(
            db_client, "get", supabase_endpoint(table),
            headers=headers, params={"select": select}, timeout=120,
        )
        res.raise_for_status()
        page = res.json() or []
        rows.extend(page)
        if len(page) < REST_PAGE_SIZE:
            break
        offset += REST_PAGE_SIZE
    return rows


def fetch_candidates_rest(db_client: httpx.Client, limit: Optional[int]) -> List[Dict[str, Any]]:
    # Already-parsed hashes, keyed by trial_id (paged — may exceed 1000 rows).
    parsed: Dict[str, Optional[str]] = {}
    for row in _rest_get_all(db_client, CRITERIA_TABLE, "trial_id,content_hash"):
        parsed[row.get("trial_id")] = row.get("content_hash")

    candidates: List[Dict[str, Any]] = []
    for row in _rest_get_all(db_client, TRIALS_TABLE, "nct_id,raw_eligibility,content_hash"):
        nct_id = row.get("nct_id")
        if nct_id in parsed and parsed[nct_id] == row.get("content_hash"):
            continue  # unchanged, already parsed
        candidates.append({
            "nct_id": nct_id,
            "raw_eligibility": row.get("raw_eligibility"),
            "content_hash": row.get("content_hash"),
        })

    candidates.sort(key=lambda c: c["nct_id"] or "")
    if limit is not None:
        candidates = candidates[:limit]
    return candidates


# --- Upsert ------------------------------------------------------------------

def upsert_criteria_direct(pg_conn: Any, trial_id: str, record: Dict[str, Any],
                           content_hash: Optional[str], parse_status: str, json_adapter: Any) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO eligibility_criteria ("
            "trial_id, cancer_type, stages, histology, histology_excluded, "
            "biomarkers_required, biomarkers_excluded, prior_treatments_required, "
            "prior_treatments_excluded, ecog_max, notes, content_hash, parse_status, parsed_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (trial_id) DO UPDATE SET "
            "cancer_type = EXCLUDED.cancer_type, "
            "stages = EXCLUDED.stages, "
            "histology = EXCLUDED.histology, "
            "histology_excluded = EXCLUDED.histology_excluded, "
            "biomarkers_required = EXCLUDED.biomarkers_required, "
            "biomarkers_excluded = EXCLUDED.biomarkers_excluded, "
            "prior_treatments_required = EXCLUDED.prior_treatments_required, "
            "prior_treatments_excluded = EXCLUDED.prior_treatments_excluded, "
            "ecog_max = EXCLUDED.ecog_max, "
            "notes = EXCLUDED.notes, "
            "content_hash = EXCLUDED.content_hash, "
            "parse_status = EXCLUDED.parse_status, "
            "parsed_at = now()",
            (
                trial_id,
                record["cancer_type"],
                record["stages"],
                record["histology"],
                record["histology_excluded"],
                json_adapter(record["biomarkers_required"]),
                json_adapter(record["biomarkers_excluded"]),
                record["prior_treatments_required"],
                record["prior_treatments_excluded"],
                record["ecog_max"],
                record["notes"],
                content_hash,
                parse_status,
            ),
        )
        pg_conn.commit()


def upsert_criteria_rest(db_client: httpx.Client, trial_id: str, record: Dict[str, Any],
                         content_hash: Optional[str], parse_status: str) -> None:
    payload = {
        "trial_id": trial_id,
        **record,
        "content_hash": content_hash,
        "parse_status": parse_status,
    }
    headers = {**supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    res = supabase_request(
        db_client, "post", supabase_endpoint(CRITERIA_TABLE),
        headers=headers, json=payload, timeout=30,
    )
    res.raise_for_status()


# --- Orchestration -----------------------------------------------------------

def run_parse(dry_run: bool = False, limit: Optional[int] = None) -> None:
    db_client: Optional[httpx.Client] = None
    pg_conn = None
    json_adapter = None
    use_rest = False

    # DB path precedence mirrors ingest.py.
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
        db_client = httpx.Client()
        use_rest = True
    elif DATABASE_URL:
        pg_conn, json_adapter = get_pg_connection()
    elif SUPABASE_URL and SUPABASE_KEY:
        db_client = httpx.Client()
        use_rest = True
        logger.warning(
            "Using publishable SUPABASE_KEY for DB access. Reads/writes may fail under RLS. "
            "Set SUPABASE_SERVICE_ROLE or DATABASE_URL for reliable parsing."
        )
    else:
        logger.error(
            "No DB configuration found. Set SUPABASE_SERVICE_ROLE or DATABASE_URL."
        )
        return

    try:
        if use_rest:
            candidates = fetch_candidates_rest(db_client, limit)
        else:
            candidates = fetch_candidates_direct(pg_conn, limit)
    except Exception:
        logger.exception("Failed to fetch candidate trials")
        return

    logger.info("Found %d trial(s) needing parse%s.", len(candidates),
                f" (limit={limit})" if limit is not None else "")

    client = get_openai_client()
    counts = {"parsed": 0, "failed": 0, "skipped": 0}

    for cand in candidates:
        nct_id = cand["nct_id"]
        raw = cand.get("raw_eligibility")
        content_hash = cand.get("content_hash")

        if not raw or not str(raw).strip():
            logger.info("%s: empty raw_eligibility, skipping", nct_id)
            counts["skipped"] += 1
            continue

        try:
            data = parse_eligibility(client, raw)
            record, status = normalize_result(data, raw)
        except json.JSONDecodeError as e:
            logger.warning("%s: malformed JSON from model (%s); storing raw in notes", nct_id, e)
            record, status = empty_result(raw)
        except Exception as e:
            logger.warning("%s: parse failed (%s); storing raw in notes", nct_id, e)
            record, status = empty_result(raw)

        if dry_run:
            logger.info("%s [DRY-RUN] status=%s parsed=%s", nct_id, status,
                        json.dumps(record, ensure_ascii=False))
            counts[status] = counts.get(status, 0) + 1
            continue

        try:
            if use_rest:
                upsert_criteria_rest(db_client, nct_id, record, content_hash, status)
            else:
                upsert_criteria_direct(pg_conn, nct_id, record, content_hash, status, json_adapter)
            counts[status] = counts.get(status, 0) + 1
        except Exception:
            logger.exception("%s: failed to upsert eligibility_criteria", nct_id)
            counts["failed"] += 1

    logger.info(
        "Parse complete. Parsed=%d Failed=%d Skipped=%d",
        counts["parsed"], counts["failed"], counts["skipped"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parse trial eligibility text into structured eligibility_criteria via GPT-4o-mini"
    )
    parser.add_argument("--dry-run", action="store_true", help="Call the LLM and log output but don't write to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max number of trials to parse this run")
    args = parser.parse_args()

    run_parse(dry_run=args.dry_run, limit=args.limit)

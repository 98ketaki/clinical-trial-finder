"""Embedding generation.

Stage 3 of the pipeline. For each trial whose eligibility was successfully parsed
(eligibility_criteria.parse_status='parsed'), build a canonical natural-language
text (see backend/text.py) and embed it with OpenAI text-embedding-ada-002
(1536-dim), upserting into trial_embeddings. Re-embeds only trials that are new or
whose content_hash changed, so it's safe to run on the weekly sync after parse.py.

Requires DATABASE_URL (pgvector). Run after ingest.py and parse.py.
"""

import os
import argparse
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from dotenv import load_dotenv

from backend.db import get_connection
from backend.text import build_trial_text

load_dotenv()

logger = logging.getLogger("ctgov_embed")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

EMBEDDING_MODEL = "text-embedding-ada-002"
EMBEDDING_DIM = 1536

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def get_openai_client():
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai is required. Install it with `pip install openai`.") from e
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured in the environment.")
    return OpenAI(api_key=OPENAI_API_KEY)


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
def embed_text(client: Any, text: str) -> List[float]:
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return resp.data[0].embedding


def fetch_candidates(conn: Any, limit: Optional[int]) -> List[Dict[str, Any]]:
    """Parsed trials with no embedding, or whose content_hash changed."""
    sql = (
        "SELECT t.nct_id, t.title, t.conditions, t.content_hash, "
        "       e.cancer_type, e.stages, e.histology, e.histology_excluded, "
        "       e.biomarkers_required, e.biomarkers_excluded, "
        "       e.prior_treatments_required, e.prior_treatments_excluded, "
        "       e.ecog_max, e.notes "
        "FROM trials t "
        "JOIN eligibility_criteria e ON e.trial_id = t.nct_id "
        "LEFT JOIN trial_embeddings em ON em.trial_id = t.nct_id "
        "WHERE e.parse_status = 'parsed' "
        "  AND (em.trial_id IS NULL OR em.content_hash IS DISTINCT FROM t.content_hash) "
        "ORDER BY t.nct_id"
    )
    params: Tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "trial": {"title": r[1], "conditions": r[2]},
            "nct_id": r[0],
            "content_hash": r[3],
            "criteria": {
                "cancer_type": r[4],
                "stages": r[5],
                "histology": r[6],
                "histology_excluded": r[7],
                "biomarkers_required": r[8],
                "biomarkers_excluded": r[9],
                "prior_treatments_required": r[10],
                "prior_treatments_excluded": r[11],
                "ecog_max": r[12],
                "notes": r[13],
            },
        })
    return out


def upsert_embedding(conn: Any, trial_id: str, embedding: List[float], content: str,
                     content_hash: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trial_embeddings (trial_id, embedding, content, content_hash) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (trial_id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, "
            "content = EXCLUDED.content, "
            "content_hash = EXCLUDED.content_hash",
            (trial_id, embedding, content, content_hash),
        )
        conn.commit()


def run_embed(dry_run: bool = False, limit: Optional[int] = None) -> None:
    conn, _ = get_connection()
    try:
        candidates = fetch_candidates(conn, limit)
    except Exception:
        logger.exception("Failed to fetch candidate trials")
        conn.close()
        return

    logger.info("Found %d trial(s) needing embedding%s.", len(candidates),
                f" (limit={limit})" if limit is not None else "")

    client = get_openai_client()
    counts = {"embedded": 0, "skipped": 0, "failed": 0}

    for cand in candidates:
        nct_id = cand["nct_id"]
        text = build_trial_text(cand["trial"], cand["criteria"])

        if not text.strip():
            logger.info("%s: empty trial text, skipping", nct_id)
            counts["skipped"] += 1
            continue

        try:
            vector = embed_text(client, text)
        except Exception:
            logger.exception("%s: embedding call failed", nct_id)
            counts["failed"] += 1
            continue

        if dry_run:
            logger.info("%s [DRY-RUN] dim=%d text=%s", nct_id, len(vector),
                        json.dumps(text[:200]))
            counts["embedded"] += 1
            continue

        try:
            upsert_embedding(conn, nct_id, vector, text, cand["content_hash"])
            counts["embedded"] += 1
        except Exception:
            logger.exception("%s: failed to upsert embedding", nct_id)
            counts["failed"] += 1

    conn.close()
    logger.info("Embed complete. Embedded=%d Skipped=%d Failed=%d",
                counts["embedded"], counts["skipped"], counts["failed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Embed parsed trial eligibility into trial_embeddings via OpenAI ada-002"
    )
    parser.add_argument("--dry-run", action="store_true", help="Call the API but don't write to DB")
    parser.add_argument("--limit", type=int, default=None, help="Max number of trials to embed this run")
    args = parser.parse_args()

    run_embed(dry_run=args.dry_run, limit=args.limit)

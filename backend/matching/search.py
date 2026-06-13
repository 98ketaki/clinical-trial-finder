"""Stage 2: semantic ranking via pgvector cosine similarity.

Embeds the patient profile as natural language (same phrasing as trials, see
backend/text.py) and ranks the hard-filtered eligible trials by cosine similarity
against trial_embeddings. Returns the top-k (nct_id, similarity) pairs.
"""

import os
from typing import Any, List, Optional, Tuple

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from dotenv import load_dotenv

from backend.text import build_profile_text

load_dotenv()

EMBEDDING_MODEL = "text-embedding-ada-002"
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
def embed_profile(client: Any, profile: dict) -> List[float]:
    text = build_profile_text(profile)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text or "lung cancer patient")
    return resp.data[0].embedding


def rank(conn: Any, profile: dict, eligible_ids: List[str], k: int = 10,
         client: Optional[Any] = None) -> List[Tuple[str, float]]:
    """Rank eligible_ids by cosine similarity to the patient profile. Top-k first."""
    if not eligible_ids:
        return []

    if client is None:
        client = get_openai_client()
    vector = embed_profile(client, profile)

    # `<=>` is cosine distance; similarity = 1 - distance.
    sql = (
        "SELECT trial_id, 1 - (embedding <=> %s::vector) AS similarity "
        "FROM trial_embeddings "
        "WHERE trial_id = ANY(%s) "
        "ORDER BY embedding <=> %s::vector "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (vector, eligible_ids, vector, k))
        rows = cur.fetchall()

    return [(r[0], float(r[1])) for r in rows]

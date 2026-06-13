"""Direct Postgres access for the matching layer.

Embeddings and vector search need pgvector, which requires a real Postgres
connection (the Supabase REST API can't run `<=>` cosine search). ingest.py and
parse.py keep their dual REST-or-PG path; everything in the serving path goes
through here.
"""

import os
from typing import Any, Tuple

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    """Open a psycopg2 connection with the pgvector adapter registered.

    Returns (conn, Json) where Json is psycopg2.extras.Json for JSONB params.
    """
    try:
        import psycopg2
        from psycopg2.extras import Json
    except ImportError as e:
        raise RuntimeError(
            "psycopg2 is required. Install it with `pip install psycopg2-binary`."
        ) from e

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured. The matching layer needs direct Postgres "
            "access for pgvector (the REST API cannot run cosine search)."
        )

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False

    # Register the pgvector type so embeddings round-trip as Python lists.
    try:
        from pgvector.psycopg2 import register_vector

        register_vector(conn)
    except ImportError as e:
        raise RuntimeError(
            "pgvector is required. Install it with `pip install pgvector`."
        ) from e

    return conn, Json

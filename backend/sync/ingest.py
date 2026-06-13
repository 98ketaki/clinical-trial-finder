import os
import argparse
import json
import logging
import hashlib
import time
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ctgov_ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CT_API_URL = "https://clinicaltrials.gov/api/v2/studies"
PAGE_SIZE = 100
SUPABASE_TABLE = "trials"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
DATABASE_URL = os.getenv("DATABASE_URL")

if not SUPABASE_URL and not DATABASE_URL:
    logger.warning("SUPABASE_URL or DATABASE_URL is required. DB operations will fail until configured.")
elif SUPABASE_URL and not SUPABASE_SERVICE_ROLE and not DATABASE_URL:
    logger.warning(
        "Only SUPABASE_KEY is configured. This is usually a publishable key and may be blocked by RLS for insert/update operations. "
        "Set SUPABASE_SERVICE_ROLE in .env or provide DATABASE_URL to perform ingestion safely."
    )


def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def supabase_headers() -> Dict[str, str]:
    key = SUPABASE_SERVICE_ROLE or SUPABASE_KEY
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_pg_connection():
    try:
        import psycopg2
        from psycopg2.extras import Json
    except ImportError as e:
        raise RuntimeError(
            "psycopg2 is required for direct DATABASE_URL ingestion. Install it with `pip install psycopg2-binary`."
        ) from e

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured for direct Postgres ingestion.")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn, Json



def supabase_endpoint() -> str:
    return f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_TABLE}"


def safe_get(d: Dict[str, Any], path: List[str]) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
        if cur is None:
            return None
    return cur


def extract_age(age_str: Optional[str]) -> Optional[int]:
    if not age_str:
        return None
    # Expect formats like "18 Years" or "18 years". Extract leading integer.
    import re

    m = re.search(r"(\d+)", age_str)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def normalize_date(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None

    import re
    from datetime import datetime

    # Strict ISO date formats first.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass

    # Year-month formats like 2026-05 or 2026/05.
    m = re.match(r"^(\d{4})[-/.](\d{1,2})$", value)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return datetime(year, month, 1).date().isoformat()

    # Year only.
    m = re.match(r"^(\d{4})$", value)
    if m:
        return f"{m.group(1)}-01-01"

    # Month name + year like "May 2026".
    m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", value)
    if m:
        month_names = {
            'january': 1,
            'february': 2,
            'march': 3,
            'april': 4,
            'may': 5,
            'june': 6,
            'july': 7,
            'august': 8,
            'september': 9,
            'october': 10,
            'november': 11,
            'december': 12,
        }
        month = month_names.get(m.group(1).lower())
        year = int(m.group(2))
        if month:
            return datetime(year, month, 1).date().isoformat()

    # Fallback: try extracting year-month-day from within the string.
    m = re.search(r"(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", value)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 1
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    return None


def extract_interventions(study: Dict[str, Any]) -> List[str]:
    # Try common locations for intervention names in CT.gov v2
    names = []
    # interventionModule -> interventionList -> intervention[] -> interventionName
    intr_list = safe_get(study, ["protocolSection", "interventionModule", "interventionList"])
    if isinstance(intr_list, dict):
        intrs = intr_list.get("intervention") or intr_list.get("interventionList") or intr_list.get("interventionNames")
        if isinstance(intrs, list):
            for it in intrs:
                if isinstance(it, dict):
                    n = it.get("interventionName") or it.get("name")
                    if n:
                        names.append(n)
    # fallback: search top-level for any "interventionName" keys
    def walk_for_names(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == "interventionname" and isinstance(v, str):
                    names.append(v)
                else:
                    walk_for_names(v)
        elif isinstance(obj, list):
            for i in obj:
                walk_for_names(i)

    if not names:
        walk_for_names(study)

    # dedupe preserving order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def extract_conditions(study: Dict[str, Any]) -> List[str]:
    conds = safe_get(study, ["protocolSection", "conditionsModule", "conditionList"])
    out = []
    if isinstance(conds, dict):
        items = conds.get("condition") or conds.get("conditionList")
        if isinstance(items, list):
            for c in items:
                if isinstance(c, str):
                    out.append(c)
                elif isinstance(c, dict):
                    # some formats use {"condition": "Lung Cancer"}
                    name = c.get("condition") or c.get("name")
                    if name:
                        out.append(name)
    # fallback: try simple path
    if not out:
        simple = safe_get(study, ["protocolSection", "identificationModule", "conditionList"])
        if isinstance(simple, list):
            out.extend([c for c in simple if isinstance(c, str)])

    return out


def extract_locations(study: Dict[str, Any]) -> Any:
    locs = safe_get(study, ["protocolSection", "contactsLocationsModule", "locations"])
    if locs is None:
        # try other keys
        locs = safe_get(study, ["protocolSection", "contactsLocationsModule"])
    return locs


def extract_raw_eligibility(study: Dict[str, Any]) -> str:
    raw = safe_get(study, ["protocolSection", "eligibilityModule", "eligibilityCriteria"])
    if isinstance(raw, str):
        return raw
    # fallback to full eligibility module JSON
    em = safe_get(study, ["protocolSection", "eligibilityModule"])
    if em is not None:
        return json.dumps(em, ensure_ascii=False)
    return ""


def extract_nct_id(study: Dict[str, Any]) -> Optional[str]:
    n = safe_get(study, ["protocolSection", "identificationModule", "nctId"]) or safe_get(study, ["idInfo", "nctId"]) or study.get("nct_id")
    return n


def parse_study(study: Dict[str, Any]) -> Dict[str, Any]:
    nct_id = extract_nct_id(study)
    title = safe_get(study, ["protocolSection", "identificationModule", "briefTitle"]) or safe_get(study, ["protocolSection", "identificationModule", "officialTitle"]) or study.get("title")
    overall_status = safe_get(study, ["protocolSection", "statusModule", "overallStatus"]) or study.get("overall_status")
    phase = safe_get(study, ["protocolSection", "designModule", "phaseList"]) or study.get("phase")
    conditions = extract_conditions(study)
    interventions = extract_interventions(study)
    sex = safe_get(study, ["protocolSection", "eligibilityModule", "gender"]) or study.get("sex")
    min_age = extract_age(safe_get(study, ["protocolSection", "eligibilityModule", "minimumAge"]))
    max_age = extract_age(safe_get(study, ["protocolSection", "eligibilityModule", "maximumAge"]))
    locations = extract_locations(study)
    raw_eligibility = extract_raw_eligibility(study)
    last_updated_raw = safe_get(study, ["protocolSection", "identificationModule", "lastUpdateSubmitDate"]) or safe_get(study, ["protocolSection", "statusModule", "statusVerifiedDate"]) or study.get("last_updated")
    last_updated = normalize_date(last_updated_raw)

    content_hash = md5_hash(raw_eligibility or "")

    return {
        "nct_id": nct_id,
        "title": title,
        "overall_status": overall_status,
        "phase": phase,
        "conditions": conditions,
        "interventions": interventions,
        "sex": sex,
        "min_age": min_age,
        "max_age": max_age,
        "locations": locations,
        "raw_eligibility": raw_eligibility,
        "last_updated": last_updated,
        "content_hash": content_hash,
    }


@retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=1, min=1, max=60), retry=retry_if_exception_type(httpx.HTTPError))
def fetch_page(client: httpx.Client, params: Dict[str, Any]) -> Dict[str, Any]:
    resp = client.get(CT_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=60),
    retry=retry_if_exception_type(httpx.TransportError),
)
def supabase_request(client: httpx.Client, method: str, *args: Any, **kwargs: Any) -> httpx.Response:
    response = getattr(client, method)(*args, **kwargs)
    return response


def upsert_trial(db_client: Optional[httpx.Client], record: Dict[str, Any], dry_run: bool = False, pg_conn: Optional[Any] = None, json_adapter: Optional[Any] = None) -> str:
    nct_id = record.get("nct_id")
    if not nct_id:
        return "skipped"

    if dry_run:
        return "added"

    if pg_conn is not None:
        return upsert_trial_direct(pg_conn, record, json_adapter=json_adapter)

    endpoint = supabase_endpoint()
    headers = supabase_headers()
    params = {"select": "content_hash", "nct_id": f"eq.{nct_id}"}

    try:
        res = supabase_request(db_client, "get", endpoint, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        logger.error("Supabase lookup failed for %s: %s", nct_id, e)
        raise

    existing = None
    if isinstance(data, list) and len(data) > 0:
        existing = data[0]

    payload = {
        "nct_id": record.get("nct_id"),
        "title": record.get("title"),
        "overall_status": record.get("overall_status"),
        "phase": record.get("phase"),
        "conditions": record.get("conditions"),
        "interventions": record.get("interventions"),
        "sex": record.get("sex"),
        "min_age": record.get("min_age"),
        "max_age": record.get("max_age"),
        "locations": record.get("locations"),
        "raw_eligibility": record.get("raw_eligibility"),
        "last_updated": record.get("last_updated"),
        "content_hash": record.get("content_hash"),
    }

    if existing is None:
        insert_headers = {**headers, "Prefer": "return=minimal"}
        res = supabase_request(db_client, "post", endpoint, headers=insert_headers, json=payload, timeout=30)
        res.raise_for_status()
        return "added"

    existing_hash = existing.get("content_hash")
    if existing_hash != record.get("content_hash"):
        update_headers = {**headers, "Prefer": "return=minimal"}
        update_params = {"nct_id": f"eq.{nct_id}"}
        res = supabase_request(db_client, "patch", endpoint, headers=update_headers, params=update_params, json=payload, timeout=30)
        res.raise_for_status()
        return "updated"

    return "skipped"


def upsert_trial_direct(pg_conn: Any, record: Dict[str, Any], json_adapter: Any) -> str:
    nct_id = record["nct_id"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash FROM trials WHERE nct_id = %s",
            (nct_id,),
        )
        existing = cur.fetchone()

        if existing is None:
            cur.execute(
                "INSERT INTO trials (nct_id, title, overall_status, phase, conditions, interventions, sex, min_age, max_age, locations, raw_eligibility, last_updated, content_hash, last_synced_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (
                    record.get("nct_id"),
                    record.get("title"),
                    record.get("overall_status"),
                    record.get("phase"),
                    record.get("conditions"),
                    record.get("interventions"),
                    record.get("sex"),
                    record.get("min_age"),
                    record.get("max_age"),
                    json_adapter(record.get("locations")),
                    record.get("raw_eligibility"),
                    record.get("last_updated"),
                    record.get("content_hash"),
                ),
            )
            pg_conn.commit()
            return "added"

        existing_hash = existing[0]
        if existing_hash != record.get("content_hash"):
            cur.execute(
                "UPDATE trials SET title = %s, overall_status = %s, phase = %s, conditions = %s, interventions = %s, sex = %s, min_age = %s, max_age = %s, locations = %s, raw_eligibility = %s, last_updated = %s, content_hash = %s, last_synced_at = now() WHERE nct_id = %s",
                (
                    record.get("title"),
                    record.get("overall_status"),
                    record.get("phase"),
                    record.get("conditions"),
                    record.get("interventions"),
                    record.get("sex"),
                    record.get("min_age"),
                    record.get("max_age"),
                    json_adapter(record.get("locations")),
                    record.get("raw_eligibility"),
                    record.get("last_updated"),
                    record.get("content_hash"),
                    record.get("nct_id"),
                ),
            )
            pg_conn.commit()
            return "updated"

        return "skipped"


def run_sync(since_date: Optional[str] = None, dry_run: bool = False):
    client = httpx.Client()
    params = {
        "query.cond": "lung cancer",
        "filter.overallStatus": "RECRUITING",
        "pageSize": PAGE_SIZE,
    }
    if since_date:
        params["query.term"] = f"AREA[LastUpdatePostDate]RANGE[{since_date},MAX]"

    db_client = None
    pg_conn = None
    json_adapter = None
    if not dry_run:
        if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
            db_client = httpx.Client()
        elif DATABASE_URL:
            pg_conn, json_adapter = get_pg_connection()
        elif SUPABASE_URL and SUPABASE_KEY:
            db_client = httpx.Client()
            logger.warning(
                "Using publishable SUPABASE_KEY for DB writes. This may fail if RLS prevents insert/update operations."
            )
        else:
            logger.error(
                "No writable DB configuration found. Set SUPABASE_SERVICE_ROLE or DATABASE_URL, or run with --dry-run."
            )
            return

    counts = {"added": 0, "updated": 0, "skipped": 0, "errors": 0}

    next_token = None
    page_num = 0
    while True:
        if next_token:
            params["pageToken"] = next_token
        logger.info("Fetching page %s", page_num + 1)
        try:
            resp_json = fetch_page(client, params)
        except Exception as e:
            logger.exception("Failed to fetch page: %s", e)
            counts["errors"] += 1
            break

        # CT.gov v2 uses 'studies' as the list key; fallbacks supported
        studies = resp_json.get("studies") or resp_json.get("data") or resp_json.get("items") or []
        if not studies:
            logger.info("No studies found on page %s", page_num + 1)
            break

        for s in studies:
            try:
                record = parse_study(s)
                result = "skipped"
                if (db_client or pg_conn) and not dry_run:
                    result = upsert_trial(db_client, record, dry_run=dry_run, pg_conn=pg_conn, json_adapter=json_adapter)
                else:
                    # simulate decision based on hash presence: treat as added for dry-run
                    result = "added"
                counts[result] = counts.get(result, 0) + 1
            except Exception:
                logger.exception("Failed processing a study")
                counts["errors"] += 1

        # pagination
        next_token = resp_json.get("nextPageToken") or resp_json.get("nextPage") or None
        page_num += 1
        if not next_token:
            break
        # be polite
        time.sleep(1)

    logger.info("Sync complete. Added=%d Updated=%d Skipped=%d Errors=%d", counts["added"], counts["updated"], counts["skipped"], counts["errors"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest ClinicalTrials.gov recruiting lung cancer trials into Supabase")
    parser.add_argument("--since_date", type=str, default=None, help="Optional since date (YYYY-MM-DD) for incremental sync")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB; just simulate")
    args = parser.parse_args()

    run_sync(since_date=args.since_date, dry_run=args.dry_run)

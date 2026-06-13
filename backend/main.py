"""FastAPI service: patient -> ranked, explained clinical-trial matches.

Endpoints:
  POST /match            patient profile -> ranked trials with explanations
  POST /feedback         thumbs up/down on a trial within a session
  GET  /trial/{nct_id}   full trial detail
  GET  /health           liveness

Privacy: patient health data lives only in patient_sessions, which expires after
2 hours and is opportunistically purged on each /match call.
"""

import os
import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.db import get_connection
from backend.models import (
    PatientProfile, TrialMatch, MatchResponse,
    FeedbackRequest, FeedbackResponse, TrialDetail,
)
from backend.matching.filters import eligible_trials
from backend.matching.search import rank
from backend.explain import explain_matches

logger = logging.getLogger("ctgov_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CTGOV_URL = "https://clinicaltrials.gov/study/{}"
TOP_K = 10
FEW_RESULTS_THRESHOLD = 3
STALENESS_NOTE = "Trial data may be up to 7 days stale; verify status on ClinicalTrials.gov."
FEW_RESULTS_PROMPT = (
    "Only these matches were found. Want to change any input and try again?"
)

# Comma-separated allowed origins, default to the Next.js dev server.
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app = FastAPI(title="Clinical Trial Finder", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _purge_expired_sessions(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM patient_sessions WHERE expires_at < now()")
    conn.commit()


def _insert_session(conn: Any, json_adapter: Any, profile: PatientProfile) -> str:
    biomarkers = [b.model_dump() for b in profile.biomarkers]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patient_sessions "
            "(cancer_type, stage, histology, biomarkers, prior_treatments, ecog, age, sex, location) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                profile.cancer_type, profile.stage, profile.histology,
                json_adapter(biomarkers), profile.prior_treatments, profile.ecog,
                profile.age, profile.sex, profile.location,
            ),
        )
        session_id = cur.fetchone()[0]
    conn.commit()
    return str(session_id)


def _staleness_note(conn: Any) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT max(last_synced_at) FROM trials")
        row = cur.fetchone()
    return STALENESS_NOTE if row and row[0] else None


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/match", response_model=MatchResponse)
def match(profile: PatientProfile) -> MatchResponse:
    conn, json_adapter = get_connection()
    try:
        _purge_expired_sessions(conn)
        session_id = _insert_session(conn, json_adapter, profile)

        profile_dict = profile.model_dump()
        eligible = eligible_trials(conn, profile_dict)
        ranked = rank(conn, profile_dict, list(eligible.keys()), k=TOP_K)

        match_dicts: List[Dict[str, Any]] = []
        for nct_id, score in ranked:
            info = eligible[nct_id]
            match_dicts.append({
                "nct_id": nct_id,
                "title": info["title"],
                "criteria": info["criteria"],
                "similarity": score,
            })

        explanations = explain_matches(profile_dict, match_dicts)

        matches: List[TrialMatch] = []
        for nct_id, score in ranked:
            info = eligible[nct_id]
            matches.append(TrialMatch(
                nct_id=nct_id,
                title=info["title"],
                phase=info["phase"],
                overall_status=info["overall_status"],
                similarity=round(score, 4),
                explanation=explanations.get(nct_id, ""),
                match_basis=info.get("match_basis", []),
                locations=info["locations"],
                ctgov_url=CTGOV_URL.format(nct_id),
            ))

        return MatchResponse(
            session_id=session_id,
            count=len(matches),
            matches=matches,
            staleness_note=_staleness_note(conn),
            few_results_prompt=(FEW_RESULTS_PROMPT if len(matches) < FEW_RESULTS_THRESHOLD else None),
        )
    finally:
        conn.close()


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest) -> FeedbackResponse:
    conn, _ = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (trial_id, session_id, rating) VALUES (%s, %s, %s)",
                (req.trial_id, req.session_id, req.rating.value),
            )
        conn.commit()
        return FeedbackResponse(ok=True)
    except Exception as e:
        conn.rollback()
        logger.exception("feedback insert failed")
        raise HTTPException(status_code=400, detail=f"Could not store feedback: {e}")
    finally:
        conn.close()


@app.get("/trial/{nct_id}", response_model=TrialDetail)
def trial_detail(nct_id: str) -> TrialDetail:
    conn, _ = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.nct_id, t.title, t.overall_status, t.phase, t.conditions, "
                "       t.interventions, t.sex, t.min_age, t.max_age, t.locations, t.raw_eligibility, "
                "       e.cancer_type, e.stages, e.histology, e.histology_excluded, "
                "       e.biomarkers_required, e.biomarkers_excluded, "
                "       e.prior_treatments_required, e.prior_treatments_excluded, e.ecog_max, e.notes "
                "FROM trials t LEFT JOIN eligibility_criteria e ON e.trial_id = t.nct_id "
                "WHERE t.nct_id = %s",
                (nct_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Trial {nct_id} not found")

        criteria = None
        if row[11] is not None or row[19] is not None or row[20] is not None:
            criteria = {
                "cancer_type": row[11], "stages": row[12], "histology": row[13],
                "histology_excluded": row[14], "biomarkers_required": row[15],
                "biomarkers_excluded": row[16], "prior_treatments_required": row[17],
                "prior_treatments_excluded": row[18], "ecog_max": row[19], "notes": row[20],
            }

        return TrialDetail(
            nct_id=row[0], title=row[1], overall_status=row[2], phase=row[3],
            conditions=row[4], interventions=row[5], sex=row[6], min_age=row[7],
            max_age=row[8], locations=row[9], raw_eligibility=row[10],
            ctgov_url=CTGOV_URL.format(row[0]), criteria=criteria,
        )
    finally:
        conn.close()

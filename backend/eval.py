"""Live eval harness for the matching engine.

Runs curated patient profiles (tests/test_cases.json) through the real
filters -> pgvector search pipeline against the live DB and checks:

  - INVARIANTS (hard): every returned trial is actually eligible for the profile.
    Re-derived independently from raw DB rows here, NOT by trusting filters.py, so
    a divergence between the SQL filter and the spec is caught.
  - EXPECTATIONS (hard): min_results / expect_zero / few_results_prompt.
  - RELEVANCE (soft): top similarity and whether a required biomarker surfaced.
    Reported as a quality scorecard; never fails the run (a genuinely relevant
    trial may carry no biomarker requirement).

Skips Claude explanations — they aren't part of match correctness.
Needs DATABASE_URL + OPENAI_API_KEY. Exit code is non-zero iff a hard check fails.
"""

import os
import sys
import json
import argparse
import logging
from typing import Any, Dict, List, Optional

from backend.db import get_connection
from backend.matching.filters import eligible_trials
from backend.matching.search import rank, get_openai_client

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

FEW_RESULTS_THRESHOLD = 3  # mirrors backend/main.py
TOP_K = 10
CASES_PATH = os.path.join(os.path.dirname(__file__), "..", "tests", "test_cases.json")


def _names(biomarkers: Any) -> set:
    out = set()
    for b in biomarkers or []:
        if isinstance(b, dict) and b.get("name"):
            out.add(str(b["name"]).strip().lower())
    return out


def _location_match(locations: Any, needle: str) -> bool:
    needle = needle.strip().lower()
    if not needle:
        return True

    def one(loc: Dict[str, Any]) -> bool:
        return any(loc.get(k) and needle in str(loc[k]).strip().lower() for k in ("city", "country"))

    if isinstance(locations, list):
        return any(isinstance(l, dict) and one(l) for l in locations)
    if isinstance(locations, dict):
        inner = locations.get("locations")
        if isinstance(inner, list):
            return any(isinstance(l, dict) and one(l) for l in inner)
        return one(locations)
    return False


def _violations(profile: Dict[str, Any], row: Dict[str, Any]) -> List[str]:
    """Independent re-check of the hard-eligibility invariants for one trial."""
    v: List[str] = []

    if (row.get("overall_status") or "").upper() != "RECRUITING":
        v.append(f"status={row.get('overall_status')} (not RECRUITING)")

    age = profile.get("age")
    if age is not None:
        lo, hi = row.get("min_age"), row.get("max_age")
        if lo is not None and age < lo:
            v.append(f"age {age} < min_age {lo}")
        if hi is not None and age > hi:
            v.append(f"age {age} > max_age {hi}")

    sex = profile.get("sex")
    if sex:
        tsex = (row.get("sex") or "").upper()
        if tsex not in ("", "ALL", sex.upper()):
            v.append(f"sex {sex} incompatible with trial sex {tsex}")

    ecog = profile.get("ecog")
    if ecog is not None:
        emax = row.get("ecog_max")
        if emax is not None and ecog > emax:
            v.append(f"ECOG {ecog} > trial max {emax}")

    patient_bms = _names(profile.get("biomarkers"))
    required = _names(row.get("biomarkers_required"))
    if required and not required.issubset(patient_bms):
        v.append(f"required biomarkers {sorted(required - patient_bms)} absent from profile")
    excluded = _names(row.get("biomarkers_excluded"))
    if excluded & patient_bms:
        v.append(f"patient has excluded biomarker(s) {sorted(excluded & patient_bms)}")

    loc = profile.get("location")
    if loc and not _location_match(row.get("locations"), loc):
        v.append(f"no location match for {loc!r}")

    return v


def fetch_rows(conn: Any, nct_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not nct_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.nct_id, t.overall_status, t.min_age, t.max_age, t.sex, t.locations, "
            "       e.ecog_max, e.biomarkers_required, e.biomarkers_excluded "
            "FROM trials t JOIN eligibility_criteria e ON e.trial_id = t.nct_id "
            "WHERE t.nct_id = ANY(%s)",
            (nct_ids,),
        )
        rows = cur.fetchall()
    cols = ["nct_id", "overall_status", "min_age", "max_age", "sex", "locations",
            "ecog_max", "biomarkers_required", "biomarkers_excluded"]
    return {r[0]: dict(zip(cols, r)) for r in rows}


def run(case_filter: Optional[str] = None, verbose: bool = False) -> int:
    with open(os.path.normpath(CASES_PATH)) as f:
        cases = json.load(f)
    if case_filter:
        cases = [c for c in cases if c["name"] == case_filter]
        if not cases:
            print(f"No case named {case_filter!r}")
            return 2

    conn, _ = get_connection()
    client = get_openai_client()

    hard_failures = 0
    rel_top_ok = 0
    rel_top_total = 0
    rel_bm_ok = 0
    rel_bm_total = 0

    print(f"{'CASE':<44} {'RESULT':<7} {'n':>4} {'top_sim':>8}  notes")
    print("-" * 96)

    try:
        for case in cases:
            name = case["name"]
            profile = case["profile"]
            expect = case.get("expect", {})

            eligible = eligible_trials(conn, profile)
            ranked = rank(conn, profile, list(eligible.keys()), k=TOP_K, client=client)
            nct_ids = [nid for nid, _ in ranked]
            rows = fetch_rows(conn, nct_ids)
            top_sim = ranked[0][1] if ranked else None
            few_prompt = len(ranked) < FEW_RESULTS_THRESHOLD

            problems: List[str] = []

            # --- invariants (hard) ---
            inv_count = 0
            for nid in nct_ids:
                for viol in _violations(profile, rows.get(nid, {})):
                    inv_count += 1
                    problems.append(f"INVARIANT {nid}: {viol}")

            # --- expectations (hard) ---
            if "min_results" in expect and len(ranked) < expect["min_results"]:
                problems.append(f"EXPECT min_results={expect['min_results']} but got {len(ranked)}")
            if expect.get("expect_zero") and len(ranked) != 0:
                problems.append(f"EXPECT zero results but got {len(ranked)}")
            if "few_results_prompt" in expect and few_prompt != expect["few_results_prompt"]:
                problems.append(f"EXPECT few_results_prompt={expect['few_results_prompt']} but got {few_prompt}")

            # --- relevance (soft, scorecard only) ---
            rel = expect.get("relevance", {})
            rel_notes = []
            if "min_top_similarity" in rel:
                rel_top_total += 1
                ok = top_sim is not None and top_sim >= rel["min_top_similarity"]
                rel_top_ok += 1 if ok else 0
                rel_notes.append(f"sim≥{rel['min_top_similarity']}:{'ok' if ok else 'LOW'}")
            if "biomarker_surfaced" in rel:
                rel_bm_total += 1
                want = rel["biomarker_surfaced"].strip().lower()
                surfaced = any(want in _names(rows.get(nid, {}).get("biomarkers_required"))
                               for nid in nct_ids)
                rel_bm_ok += 1 if surfaced else 0
                rel_notes.append(f"{rel['biomarker_surfaced']}:{'surfaced' if surfaced else 'no'}")

            hard = len(problems) > 0
            hard_failures += 1 if hard else 0
            status = "FAIL" if hard else "PASS"
            sim_str = f"{top_sim:.3f}" if top_sim is not None else "  -  "
            note = "; ".join(rel_notes)
            if inv_count:
                note = f"{inv_count} invariant viol; " + note
            print(f"{name:<44} {status:<7} {len(ranked):>4} {sim_str:>8}  {note}")

            if hard or verbose:
                for p in problems:
                    print(f"    ✗ {p}")
                if verbose:
                    for nid, sim in ranked:
                        print(f"      · {nid}  sim={sim:.3f}")
    finally:
        conn.close()

    print("-" * 96)
    print(f"Cases: {len(cases)}  |  hard failures: {hard_failures}")
    if rel_top_total:
        print(f"Relevance — top similarity threshold met: {rel_top_ok}/{rel_top_total}")
    if rel_bm_total:
        print(f"Relevance — required biomarker surfaced:   {rel_bm_ok}/{rel_bm_total}")
    print("RESULT:", "PASS" if hard_failures == 0 else "FAIL")
    return 0 if hard_failures == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live eval of the matching engine against test_cases.json")
    ap.add_argument("--case", default=None, help="Run only the case with this exact name")
    ap.add_argument("--verbose", action="store_true", help="List returned trials per case")
    args = ap.parse_args()
    sys.exit(run(case_filter=args.case, verbose=args.verbose))

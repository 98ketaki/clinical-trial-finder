"""Stage 1: hard eligibility filters.

Reduces the trial universe to the subset a patient is structurally eligible for,
before the semantic ranking in search.py. Age / sex / ECOG / status are filtered
in SQL; biomarker logic and location matching (JSONB) are done in Python.

Per AGENTS.md: never filter on location `state` (only 71% populated). Unknown
required biomarkers are treated as exclusions (conservative).
"""

from typing import Any, Dict, List, Optional


def _biomarker_names(biomarkers: Optional[List[Dict[str, Any]]]) -> set:
    out = set()
    for b in biomarkers or []:
        if isinstance(b, dict) and b.get("name"):
            out.add(str(b["name"]).strip().lower())
    return out


def _match_basis(profile: Dict[str, Any], min_age: Any, max_age: Any, sex: Any,
                 ecog_max: Any, bm_required: Any, bm_excluded: Any) -> List[Dict[str, str]]:
    """Truthful, positive checks describing which hard gates this trial satisfied.

    Only the gates that were actually applicable to this patient are included; the
    %match itself is semantic similarity computed after these gates pass.
    """
    basis: List[Dict[str, str]] = []

    age = profile.get("age")
    if age is not None:
        if min_age is None and max_age is None:
            basis.append({"label": "Age", "detail": "Trial has no age limit"})
        else:
            lo = min_age if min_age is not None else "any"
            hi = max_age if max_age is not None else "any"
            basis.append({"label": "Age", "detail": f"Age {age} is within the trial's range {lo}–{hi}"})

    if profile.get("sex"):
        if sex and str(sex).strip().upper() not in ("", "ALL"):
            basis.append({"label": "Sex", "detail": "Trial is open to your sex"})

    ecog = profile.get("ecog")
    if ecog is not None:
        if ecog_max is None:
            basis.append({"label": "ECOG", "detail": "Trial has no ECOG limit"})
        else:
            basis.append({"label": "ECOG", "detail": f"Your ECOG {ecog} ≤ trial maximum {ecog_max}"})

    required_names = _biomarker_names(bm_required)
    canonical = {b.get("name") for b in (bm_required or []) if isinstance(b, dict) and b.get("name")}
    for name in canonical:
        if str(name).strip().lower() in required_names:
            basis.append({"label": "Biomarker", "detail": f"Requires {name} — present in your profile"})

    if _biomarker_names(bm_excluded):
        basis.append({"label": "Biomarkers", "detail": "You have none of the trial's excluded biomarkers"})

    loc = profile.get("location")
    if loc:
        basis.append({"label": "Location", "detail": f"Recruiting near {loc}"})

    return basis


def _location_matches(locations: Any, patient_location: str) -> bool:
    """True if any trial location's city or country matches the patient location."""
    needle = patient_location.strip().lower()
    if not needle:
        return True

    def check_one(loc: Dict[str, Any]) -> bool:
        for key in ("city", "country"):
            val = loc.get(key)
            if val and needle in str(val).strip().lower():
                return True
        return False

    # locations is usually a list of location dicts; tolerate a dict wrapper.
    if isinstance(locations, list):
        return any(isinstance(l, dict) and check_one(l) for l in locations)
    if isinstance(locations, dict):
        inner = locations.get("locations")
        if isinstance(inner, list):
            return any(isinstance(l, dict) and check_one(l) for l in inner)
        return check_one(locations)
    return False


def eligible_trials(conn: Any, profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return {nct_id: trial+criteria fields} for trials passing all hard filters."""
    where = ["e.parse_status = 'parsed'", "t.overall_status = 'RECRUITING'"]
    params: List[Any] = []

    age = profile.get("age")
    if age is not None:
        where.append("(t.min_age IS NULL OR %s >= t.min_age)")
        where.append("(t.max_age IS NULL OR %s <= t.max_age)")
        params.extend([age, age])

    sex = profile.get("sex")
    if sex:
        where.append("(t.sex IS NULL OR upper(t.sex) = 'ALL' OR upper(t.sex) = upper(%s))")
        params.append(sex)

    ecog = profile.get("ecog")
    if ecog is not None:
        where.append("(e.ecog_max IS NULL OR %s <= e.ecog_max)")
        params.append(ecog)

    sql = (
        "SELECT t.nct_id, t.title, t.phase, t.overall_status, t.locations, t.conditions, "
        "       t.min_age, t.max_age, t.sex, "
        "       e.ecog_max, e.biomarkers_required, e.biomarkers_excluded, "
        "       e.cancer_type, e.stages, e.histology, e.histology_excluded, "
        "       e.prior_treatments_required, e.prior_treatments_excluded, e.notes "
        "FROM trials t JOIN eligibility_criteria e ON e.trial_id = t.nct_id "
        "WHERE " + " AND ".join(where)
    )

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    patient_bms = _biomarker_names(profile.get("biomarkers"))
    patient_location = profile.get("location")

    eligible: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        (nct_id, title, phase, status, locations, conditions, min_age, max_age, trial_sex,
         ecog_max, bm_required, bm_excluded, cancer_type, stages, histology, histology_excluded,
         pt_required, pt_excluded, notes) = r

        # Required biomarkers: patient must have each (by name). Unknown -> excluded.
        required_names = _biomarker_names(bm_required)
        if required_names and not required_names.issubset(patient_bms):
            continue

        # Excluded biomarkers: patient must NOT have any of them.
        excluded_names = _biomarker_names(bm_excluded)
        if excluded_names and (excluded_names & patient_bms):
            continue

        # Location: city/country match (never state).
        if patient_location and not _location_matches(locations, patient_location):
            continue

        eligible[nct_id] = {
            "title": title,
            "phase": phase,
            "overall_status": status,
            "locations": locations,
            "conditions": conditions,
            "match_basis": _match_basis(
                profile, min_age, max_age, trial_sex, ecog_max, bm_required, bm_excluded
            ),
            "criteria": {
                "cancer_type": cancer_type,
                "stages": stages,
                "histology": histology,
                "histology_excluded": histology_excluded,
                "biomarkers_required": bm_required,
                "biomarkers_excluded": bm_excluded,
                "prior_treatments_required": pt_required,
                "prior_treatments_excluded": pt_excluded,
                "ecog_max": ecog_max,
                "notes": notes,
            },
        }
    return eligible

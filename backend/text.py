"""Shared natural-language text builders for embeddings.

Both trials and patient profiles must be rendered into the SAME phrasing so their
embeddings live in a comparable space — semantic cosine search only works if the
patient text and the trial text are described the same way. embed.py uses
`build_trial_text`; search.py uses `build_profile_text`.
"""

from typing import Any, Dict, List, Optional


def _join(label: str, values: Optional[List[Any]]) -> Optional[str]:
    if not values:
        return None
    cleaned = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not cleaned:
        return None
    return f"{label}: {', '.join(cleaned)}."


def _biomarker_phrases(biomarkers: Optional[List[Dict[str, Any]]]) -> List[str]:
    out: List[str] = []
    for b in biomarkers or []:
        if not isinstance(b, dict):
            continue
        name = b.get("name")
        if not name:
            continue
        status = b.get("status") or "any"
        out.append(f"{name} {status}")
    return out


def build_trial_text(trial: Dict[str, Any], criteria: Dict[str, Any]) -> str:
    """Render a trial + its parsed eligibility into the canonical embedding text."""
    parts: List[str] = []

    title = trial.get("title")
    if title:
        parts.append(f"Clinical trial: {title}.")

    cancer_type = criteria.get("cancer_type")
    if cancer_type:
        parts.append(f"Cancer type: {cancer_type}.")

    for label, key in (
        ("Eligible stages", "stages"),
        ("Histology", "histology"),
        ("Excluded histology", "histology_excluded"),
        ("Required prior treatments", "prior_treatments_required"),
        ("Excluded prior treatments", "prior_treatments_excluded"),
    ):
        phrase = _join(label, criteria.get(key))
        if phrase:
            parts.append(phrase)

    req = _biomarker_phrases(criteria.get("biomarkers_required"))
    if req:
        parts.append(f"Required biomarkers: {', '.join(req)}.")
    exc = _biomarker_phrases(criteria.get("biomarkers_excluded"))
    if exc:
        parts.append(f"Excluded biomarkers: {', '.join(exc)}.")

    ecog_max = criteria.get("ecog_max")
    if ecog_max is not None:
        parts.append(f"Maximum ECOG performance status: {ecog_max}.")

    conditions = trial.get("conditions")
    cond_phrase = _join("Conditions", conditions)
    if cond_phrase:
        parts.append(cond_phrase)

    notes = criteria.get("notes")
    if notes:
        parts.append(f"Notes: {notes}")

    return " ".join(parts).strip()


def build_profile_text(profile: Dict[str, Any]) -> str:
    """Render a patient profile into the canonical embedding text (mirrors trials)."""
    parts: List[str] = []

    cancer_type = profile.get("cancer_type")
    if cancer_type:
        parts.append(f"Cancer type: {cancer_type}.")

    stage = profile.get("stage")
    if stage:
        parts.append(f"Eligible stages: {stage}.")

    histology = profile.get("histology")
    if histology:
        parts.append(f"Histology: {histology}.")

    treatments = _join("Required prior treatments", profile.get("prior_treatments"))
    if treatments:
        parts.append(treatments)

    bms = _biomarker_phrases(profile.get("biomarkers"))
    if bms:
        parts.append(f"Required biomarkers: {', '.join(bms)}.")

    ecog = profile.get("ecog")
    if ecog is not None:
        parts.append(f"Maximum ECOG performance status: {ecog}.")

    return " ".join(parts).strip()

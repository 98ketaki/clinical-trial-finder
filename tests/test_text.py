"""Unit tests for backend/text.py — patient and trial text must share phrasing.

Semantic cosine search only works if both sides of the embedding use the same
labels, so these tests pin that parallelism.
"""

from backend.text import build_trial_text, build_profile_text


def test_trial_and_profile_use_parallel_labels():
    trial = build_trial_text(
        {"title": "Study X", "conditions": ["NSCLC"]},
        {"cancer_type": "lung cancer", "stages": ["IV"], "histology": ["adenocarcinoma"],
         "biomarkers_required": [{"name": "EGFR", "status": "positive"}], "ecog_max": 2},
    )
    profile = build_profile_text(
        {"cancer_type": "lung cancer", "stage": "IV", "histology": "adenocarcinoma",
         "biomarkers": [{"name": "EGFR", "status": "positive"}], "ecog": 1},
    )
    for label in ("Cancer type:", "Eligible stages:", "Histology:",
                  "Required biomarkers:", "Maximum ECOG performance status:"):
        assert label in trial, f"trial text missing {label!r}"
        assert label in profile, f"profile text missing {label!r}"
    assert "EGFR positive" in trial and "EGFR positive" in profile


def test_empty_profile_is_empty_string():
    assert build_profile_text({}) == ""


def test_trial_text_includes_title_and_conditions():
    txt = build_trial_text({"title": "Lung Trial", "conditions": ["NSCLC", "Adenocarcinoma"]}, {})
    assert "Lung Trial" in txt and "Conditions: NSCLC, Adenocarcinoma." in txt

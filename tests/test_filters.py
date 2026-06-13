"""Unit tests for the pure logic in backend/matching/filters.py (no DB)."""

from backend.matching.filters import _biomarker_names, _location_matches, _match_basis


def test_biomarker_names_lowercases_and_dedupes():
    assert _biomarker_names([{"name": "EGFR"}, {"name": "alk"}, {"name": "EGFR"}]) == {"egfr", "alk"}
    assert _biomarker_names([{"status": "positive"}]) == set()  # no name
    assert _biomarker_names(None) == set()


def test_location_matches_city_or_country():
    locs = [{"city": "Boston", "country": "United States"}]
    assert _location_matches(locs, "boston") is True
    assert _location_matches(locs, "united states") is True
    assert _location_matches(locs, "paris") is False


def test_location_empty_needle_matches():
    assert _location_matches([{"city": "Boston"}], "") is True


def test_location_never_uses_state():
    # state is intentionally not consulted (71% populated per AGENTS.md)
    assert _location_matches([{"city": "Houston", "state": "Texas"}], "texas") is False


def test_match_basis_age_within_range():
    basis = _match_basis({"age": 60}, 18, 75, None, None, None, None)
    labels = {f["label"] for f in basis}
    assert "Age" in labels
    assert any("18" in f["detail"] and "75" in f["detail"] for f in basis if f["label"] == "Age")


def test_match_basis_skips_blank_fields():
    # profile gives nothing -> no factors
    assert _match_basis({}, 18, 75, "ALL", 2, None, None) == []


def test_match_basis_ecog_and_biomarker():
    basis = _match_basis(
        {"ecog": 1, "biomarkers": [{"name": "EGFR"}]},
        None, None, None, 2,
        [{"name": "EGFR", "status": "positive"}], None,
    )
    details = " ".join(f["detail"] for f in basis)
    assert "ECOG 1" in details and "2" in details
    assert "EGFR" in details


def test_match_basis_excluded_biomarker_note():
    basis = _match_basis({"biomarkers": []}, None, None, None, None, None,
                         [{"name": "ALK", "status": "positive"}])
    assert any("excluded biomarkers" in f["detail"] for f in basis)

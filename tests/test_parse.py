"""Unit tests for the pure parsing logic in backend/sync/parse.py (no DB/API)."""

import backend.sync.parse as p


def test_strip_markdown_escapes():
    assert p.strip_markdown_escapes(r"ECOG \<= 2, age \>= 18, x\^2") == "ECOG <= 2, age >= 18, x^2"


def test_expand_stages_synonyms():
    assert p._expand_stages(["metastatic"]) == ["IV"]
    assert p._expand_stages(["extensive stage"]) == ["IV"]
    assert p._expand_stages(["locally advanced"]) == ["III"]
    assert p._expand_stages(["limited stage"]) == ["I", "II", "III"]
    assert p._expand_stages(["unresectable"]) == ["III", "IV"]


def test_expand_stages_roman_and_order():
    # "Stage IIIA" normalizes to III; output is canonical-ordered and deduped
    assert p._expand_stages(["Stage IIIA", "IV", "metastatic"]) == ["III", "IV"]


def test_biomarkers_canonical_only_with_default_status():
    bm = p._as_biomarker_list([
        {"name": "egfr", "status": "mutated"},
        {"name": "not-a-real-marker", "status": "x"},  # dropped
        {"name": "KRAS_G12C"},                          # default status
    ])
    assert bm == [{"name": "EGFR", "status": "mutated"}, {"name": "KRAS_G12C", "status": "any"}]


def test_as_ecog():
    assert p._as_ecog("ECOG 0-2") == 0
    assert p._as_ecog("2") == 2
    assert p._as_ecog(1) == 1
    assert p._as_ecog(None) is None
    assert p._as_ecog(True) is None  # bool is not a valid ecog


def test_normalize_and_empty_key_parity():
    data = {"cancer_type": "NSCLC", "stages": ["metastatic"], "histology": ["adenocarcinoma"],
            "biomarkers_required": [{"name": "alk", "status": "positive"}], "ecog_max": "2", "notes": "x"}
    rec, status = p.normalize_result(data, "raw")
    assert status == "parsed"
    assert rec["stages"] == ["IV"]
    assert rec["biomarkers_required"] == [{"name": "ALK", "status": "positive"}]
    assert rec["ecog_max"] == 2

    empty, est = p.empty_result("RAWTEXT")
    assert est == "failed" and empty["notes"] == "RAWTEXT"
    # failure path must produce the exact same schema keys as the success path
    assert set(rec.keys()) == set(empty.keys())

"""Pydantic v2 request/response models for the FastAPI service."""

from typing import Any, Dict, List, Optional
from enum import Enum

from pydantic import BaseModel, Field


class Biomarker(BaseModel):
    name: str
    status: str = "positive"


class PatientProfile(BaseModel):
    """Patient intake. Health data — persisted only transiently (session expires 2h)."""
    cancer_type: Optional[str] = "lung cancer"
    stage: Optional[str] = None                       # "I" | "II" | "III" | "IV"
    histology: Optional[str] = None
    biomarkers: List[Biomarker] = Field(default_factory=list)
    prior_treatments: List[str] = Field(default_factory=list)
    ecog: Optional[int] = None
    age: Optional[int] = None
    sex: Optional[str] = None                         # "MALE" | "FEMALE"
    location: Optional[str] = None                    # city or country


class MatchFactor(BaseModel):
    label: str
    detail: str


class TrialMatch(BaseModel):
    nct_id: str
    title: Optional[str] = None
    phase: Optional[str] = None
    overall_status: Optional[str] = None
    similarity: float
    explanation: str
    match_basis: List[MatchFactor] = Field(default_factory=list)
    locations: Optional[Any] = None
    ctgov_url: str


class MatchResponse(BaseModel):
    session_id: str
    count: int
    matches: List[TrialMatch]
    disclaimer: str = "This is not medical advice. Discuss any trial with your oncologist."
    staleness_note: Optional[str] = None
    few_results_prompt: Optional[str] = None


class Rating(str, Enum):
    thumbs_up = "thumbs_up"
    thumbs_down = "thumbs_down"


class FeedbackRequest(BaseModel):
    session_id: str
    trial_id: str
    rating: Rating


class FeedbackResponse(BaseModel):
    ok: bool = True


class TrialDetail(BaseModel):
    nct_id: str
    title: Optional[str] = None
    overall_status: Optional[str] = None
    phase: Optional[str] = None
    conditions: Optional[List[str]] = None
    interventions: Optional[List[str]] = None
    sex: Optional[str] = None
    min_age: Optional[int] = None
    max_age: Optional[int] = None
    locations: Optional[Any] = None
    raw_eligibility: Optional[str] = None
    ctgov_url: str
    criteria: Optional[Dict[str, Any]] = None

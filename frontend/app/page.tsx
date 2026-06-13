"use client";

import { useState } from "react";
import {
  CANONICAL_BIOMARKERS,
  HISTOLOGY_OPTIONS,
  PRIOR_TREATMENT_OPTIONS,
  MatchResponse,
  PatientProfile,
  postMatch,
} from "@/lib/api";
import TrialCard from "./TrialCard";

const STAGES = ["I", "II", "III", "IV"];
const ECOG_VALUES = [0, 1, 2, 3, 4];

export default function Home() {
  const [age, setAge] = useState("");
  const [sex, setSex] = useState("");
  const [stage, setStage] = useState("");
  const [histology, setHistology] = useState("");
  const [ecog, setEcog] = useState("");
  const [location, setLocation] = useState("");
  const [treatments, setTreatments] = useState<string[]>([]);
  const [treatmentInput, setTreatmentInput] = useState("");
  const [biomarkers, setBiomarkers] = useState<Set<string>>(new Set());
  const [showHow, setShowHow] = useState(false);

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<MatchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  function toggleBiomarker(name: string) {
    setBiomarkers((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  function addTreatment(raw: string) {
    const value = raw.trim();
    if (!value) return;
    setTreatments((prev) => (prev.includes(value) ? prev : [...prev, value]));
    setTreatmentInput("");
  }

  function removeTreatment(value: string) {
    setTreatments((prev) => prev.filter((t) => t !== value));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);

    const profile: PatientProfile = {
      cancer_type: "lung cancer",
      stage: stage || null,
      histology: histology || null,
      biomarkers: Array.from(biomarkers).map((name) => ({ name, status: "positive" })),
      prior_treatments: treatments,
      ecog: ecog === "" ? null : Number(ecog),
      age: age === "" ? null : Number(age),
      sex: sex || null,
      location: location || null,
    };

    try {
      setResult(await postMatch(profile));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  const field = "mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm";
  const label = "block text-sm font-medium text-slate-700";

  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold text-slate-900">Clinical Trial Finder</h1>
      <p className="mt-1 text-sm text-slate-600">
        Lung cancer trial matching. Enter your profile to see recruiting trials.
      </p>

      <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
        This tool is not medical advice. Always discuss any trial with your oncologist.
      </div>

      <form onSubmit={onSubmit} className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className={label}>Age</label>
          <input type="number" min={0} max={120} value={age}
            onChange={(e) => setAge(e.target.value)} className={field} placeholder="60" />
        </div>
        <div>
          <label className={label}>Sex</label>
          <select value={sex} onChange={(e) => setSex(e.target.value)} className={field}>
            <option value="">Any</option>
            <option value="FEMALE">Female</option>
            <option value="MALE">Male</option>
          </select>
        </div>
        <div>
          <label className={label}>Stage</label>
          <select value={stage} onChange={(e) => setStage(e.target.value)} className={field}>
            <option value="">Unknown</option>
            {STAGES.map((s) => <option key={s} value={s}>Stage {s}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>ECOG performance status</label>
          <select value={ecog} onChange={(e) => setEcog(e.target.value)} className={field}>
            <option value="">Unknown</option>
            {ECOG_VALUES.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>Histology</label>
          <input list="histology-options" value={histology}
            onChange={(e) => setHistology(e.target.value)}
            className={field} placeholder="Select or type…" />
          <datalist id="histology-options">
            {HISTOLOGY_OPTIONS.map((h) => <option key={h} value={h} />)}
          </datalist>
        </div>
        <div>
          <label className={label}>Location (city or country)</label>
          <input value={location} onChange={(e) => setLocation(e.target.value)}
            className={field} placeholder="Boston" />
        </div>
        <div className="sm:col-span-2">
          <label className={label}>Prior treatments</label>
          <div className="mt-1 flex gap-2">
            <input list="treatment-options" value={treatmentInput}
              onChange={(e) => setTreatmentInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); addTreatment(treatmentInput); }
              }}
              className={field} placeholder="Select or type a treatment, then Add" />
            <datalist id="treatment-options">
              {PRIOR_TREATMENT_OPTIONS.map((t) => <option key={t} value={t} />)}
            </datalist>
            <button type="button" onClick={() => addTreatment(treatmentInput)}
              className="shrink-0 rounded-md border border-slate-300 px-3 text-sm font-medium text-slate-700 hover:bg-slate-50">
              Add
            </button>
          </div>
          {treatments.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {treatments.map((t) => (
                <span key={t}
                  className="inline-flex items-center gap-1 rounded-full border border-blue-500 bg-blue-50 px-3 py-1 text-xs text-blue-700">
                  {t}
                  <button type="button" onClick={() => removeTreatment(t)}
                    className="text-blue-500 hover:text-blue-800" aria-label={`Remove ${t}`}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="sm:col-span-2">
          <label className={label}>Biomarkers</label>
          <div className="mt-2 flex flex-wrap gap-2">
            {CANONICAL_BIOMARKERS.map((b) => (
              <button type="button" key={b} onClick={() => toggleBiomarker(b)}
                className={`rounded-full border px-3 py-1 text-xs transition ${
                  biomarkers.has(b)
                    ? "border-blue-500 bg-blue-50 text-blue-700"
                    : "border-slate-300 text-slate-600 hover:bg-slate-50"
                }`}>
                {b}
              </button>
            ))}
          </div>
        </div>

        <div className="sm:col-span-2">
          <button type="submit" disabled={loading}
            className="w-full rounded-md bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-60">
            {loading ? "Finding trials…" : "Find matching trials"}
          </button>
        </div>
      </form>

      {error && (
        <div className="mt-6 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {result && (
        <section className="mt-8">
          <div className="flex items-baseline justify-between">
            <h2 className="text-lg font-semibold">
              {result.count} {result.count === 1 ? "match" : "matches"}
            </h2>
            {result.staleness_note && (
              <span className="text-xs text-slate-400">{result.staleness_note}</span>
            )}
          </div>

          <button type="button" onClick={() => setShowHow((v) => !v)}
            className="mt-1 text-xs font-medium text-blue-600 hover:underline">
            {showHow ? "Hide" : "How is the % match calculated?"}
          </button>
          {showHow && (
            <div className="mt-2 rounded-md border border-slate-200 bg-white px-4 py-3 text-xs leading-relaxed text-slate-600">
              Trials are first <strong>filtered</strong> on hard eligibility criteria — recruiting
              status, your age, sex, ECOG, required/excluded biomarkers, and location. Only trials you
              could actually enroll in remain. Those are then <strong>ranked</strong> by semantic
              similarity: how closely each trial&apos;s eligibility text matches your profile (cancer
              type, stage, histology, prior treatments). The percentage is that similarity score —
              higher means a closer textual match, not a guarantee of eligibility. Each result lists the
              specific criteria it passed below its explanation.
            </div>
          )}

          {result.few_results_prompt && (
            <div className="mt-3 rounded-md border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
              {result.few_results_prompt}
            </div>
          )}

          <div className="mt-4 space-y-4">
            {result.matches.map((m) => (
              <TrialCard key={m.nct_id} match={m} sessionId={result.session_id} />
            ))}
          </div>

          {result.count === 0 && (
            <p className="mt-4 text-sm text-slate-500">
              No recruiting trials matched your profile. Try widening your inputs (remove
              location, clear biomarkers).
            </p>
          )}

          <p className="mt-6 text-xs text-slate-400">{result.disclaimer}</p>
        </section>
      )}
    </main>
  );
}

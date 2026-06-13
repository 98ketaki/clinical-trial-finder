"use client";

import { useState } from "react";
import { TrialMatch, postFeedback, summarizeLocations } from "@/lib/api";

export default function TrialCard({
  match,
  sessionId,
}: {
  match: TrialMatch;
  sessionId: string;
}) {
  const [rated, setRated] = useState<"thumbs_up" | "thumbs_down" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function rate(rating: "thumbs_up" | "thumbs_down") {
    setError(null);
    try {
      await postFeedback(sessionId, match.nct_id, rating);
      setRated(rating);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to send feedback");
    }
  }

  const locs = summarizeLocations(match.locations);

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-4">
        <h3 className="text-lg font-semibold text-slate-900">
          {match.title || match.nct_id}
        </h3>
        <span
          title="Semantic similarity between this trial's eligibility text and your profile, ranked after hard eligibility filters. Higher = closer textual match, not a guarantee of eligibility."
          className="shrink-0 cursor-help rounded-full bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700">
          {(match.similarity * 100).toFixed(0)}% match
        </span>
      </div>

      <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-500">
        <span>{match.nct_id}</span>
        {match.phase && <span>· {match.phase}</span>}
        {match.overall_status && <span>· {match.overall_status}</span>}
        {locs && <span>· {locs}</span>}
      </div>

      <p className="mt-3 text-sm leading-relaxed text-slate-700">{match.explanation}</p>

      <div className="mt-3 rounded-md bg-slate-50 px-3 py-2">
        <p className="text-xs font-medium text-slate-600">
          Passed your hard criteria; ranked by semantic similarity ({(match.similarity * 100).toFixed(0)}%).
        </p>
        {match.match_basis.length > 0 ? (
          <ul className="mt-1.5 space-y-0.5">
            {match.match_basis.map((f, i) => (
              <li key={i} className="text-xs text-slate-600">
                <span className="text-green-600">✓</span> <span className="font-medium">{f.label}:</span>{" "}
                {f.detail}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1 text-xs text-slate-400">
            No hard filters applied — add age, ECOG, biomarkers, or location to narrow matches.
          </p>
        )}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <a
          href={match.ctgov_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm font-medium text-blue-600 hover:underline"
        >
          View on ClinicalTrials.gov →
        </a>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => rate("thumbs_up")}
            disabled={rated !== null}
            className={`rounded-md border px-3 py-1 text-sm transition ${
              rated === "thumbs_up"
                ? "border-green-500 bg-green-50 text-green-700"
                : "border-slate-200 hover:bg-slate-50"
            } disabled:cursor-default`}
            aria-label="Helpful"
          >
            👍
          </button>
          <button
            onClick={() => rate("thumbs_down")}
            disabled={rated !== null}
            className={`rounded-md border px-3 py-1 text-sm transition ${
              rated === "thumbs_down"
                ? "border-red-500 bg-red-50 text-red-700"
                : "border-slate-200 hover:bg-slate-50"
            } disabled:cursor-default`}
            aria-label="Not helpful"
          >
            👎
          </button>
        </div>
      </div>
      {rated && <p className="mt-2 text-xs text-slate-400">Thanks for the feedback.</p>}
      {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
    </div>
  );
}

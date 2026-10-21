import { useEffect, useState } from "react";
import { api, ApiError, type Config, type Job, type Report } from "../api";

export const ACTION_TEXT: Record<string, string> = {
  would_create: "New contact",
  would_update: "Updates a contact",
  would_skip_unchanged: "Already up to date",
  would_upsert: "New or updated",
  merged: "Merged into another row",
  invalid: "Won't import",
};

export function DryRunView({ job, config, onBackToReview, onImport }: {
  job: Job;
  config: Config;
  onBackToReview: () => void;
  onImport: () => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api.dryRun(job.id)
      .then((r) => { setReport(r); setError(null); })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [job.id, job.mappings]);

  if (loading) return <section className="view"><p aria-live="polite">Working out what the import would do…</p></section>;
  if (error || !report) return <section className="view"><p className="banner banner-error" role="alert">{error}</p></section>;

  const c = report.counts;
  const writes = (c.would_create ?? 0) + (c.would_update ?? 0) + (c.would_upsert ?? 0);

  return (
    <section className="view">
      <div className="view-head">
        <h1>{writes === 0 ? "Nothing to write" : `${writes} ${writes === 1 ? "contact" : "contacts"} would change`}</h1>
        <p className="lede">
          {sentence(c)} Nothing has been written to HubSpot yet.
          {report.definitions === "built-in" && " HubSpot isn't connected, so this preview checks against BridgeOps' own list of fields and can't tell new contacts from existing ones."}
        </p>
        {report.blockers.length > 0 ? (
          <div className="banner banner-needs">
            <p>Finish the review before importing:</p>
            <ul>{report.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
            <button className="btn" onClick={onBackToReview}>Back to review</button>
          </div>
        ) : (
          <div className="view-actions">
            <button className="btn btn-primary" disabled={!config.hubspot_configured || writes === 0} onClick={onImport}>
              Continue to import
            </button>
            {!config.hubspot_configured && <span className="hint">Add HUBSPOT_TOKEN to .env to import.</span>}
          </div>
        )}
      </div>

      <div className="table-scroll">
        <table className="table plan-table">
          <thead><tr><th>Row</th><th>What happens</th><th>Email</th><th>Fields sent</th></tr></thead>
          <tbody>
            {report.records.map((r) => (
              <tr key={r.row} className={`action-${r.action}`}>
                <td className="num">{r.row + 1}</td>
                <td>
                  <span className="pill">{ACTION_TEXT[r.action] ?? r.action}</span>
                  {r.merged_into !== null && <span className="hint"> row {r.merged_into + 1}</span>}
                  {r.merged_rows.length > 0 && <span className="hint"> with row {r.merged_rows.map((x) => x + 1).join(", ")}</span>}
                  {r.errors.map((e) => <p key={e} className="row-error">{e}</p>)}
                </td>
                <td>{r.email ?? <span className="blank">none</span>}</td>
                <td><div className="fields">
                  {Object.entries(r.properties).filter(([k]) => k !== "email").map(([k, v]) => (
                    <span key={k} className="field"><span className="field-name">{k}</span> {v}</span>
                  ))}
                </div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(c.rows ?? 0) > report.records.length && (
        <p className="hint">Showing the first {report.records.length} of {c.rows} rows.</p>
      )}
    </section>
  );
}

function sentence(c: Record<string, number>): string {
  const parts: string[] = [];
  if (c.would_create) parts.push(`${c.would_create} new`);
  if (c.would_update) parts.push(`${c.would_update} updated`);
  if (c.would_upsert) parts.push(`${c.would_upsert} new or updated`);
  if (c.would_skip_unchanged) parts.push(`${c.would_skip_unchanged} already up to date`);
  if (c.merged) parts.push(`${c.merged} merged as duplicates`);
  if (c.invalid) parts.push(`${c.invalid} can't be imported`);
  return parts.length ? `Of ${c.rows} rows: ${parts.join(", ")}.` : "";
}

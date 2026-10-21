import { useEffect, useState } from "react";
import { api, ApiError, type Config, type ImportRecord, type Job } from "../api";

const RECORD_TEXT: Record<string, string> = {
  created: "Created",
  updated: "Updated",
  unchanged: "Already up to date",
  failed: "Failed",
  invalid: "Not imported",
  merged: "Merged",
  rolled_back: "Rolled back",
  rollback_failed: "Rollback failed",
  rollback_conflict: "Changed in HubSpot since, left as is",
};

export function ImportView({ job, config, onChanged }: {
  job: Job;
  config: Config;
  onChanged: () => Promise<void>;
}) {
  const [records, setRecords] = useState<ImportRecord[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<"import" | "rollback" | null>(null);
  const [error, setError] = useState<{ message: string; details: string[] } | null>(null);
  const [filter, setFilter] = useState<string>("all");

  useEffect(() => { api.records(job.id).then(setRecords).catch(() => setRecords([])); }, [job.id, job.status]);

  const run = async (kind: "import" | "rollback") => {
    setConfirming(null); setError(null);
    setBusy(kind === "import" ? "Importing into HubSpot" : "Rolling back");
    try {
      await (kind === "import" ? api.execute(job.id) : api.rollback(job.id));
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? { message: e.message, details: e.details } : { message: String(e), details: [] });
    } finally {
      setBusy(null);
    }
  };

  const counts = records.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.status]: (acc[r.status] ?? 0) + 1 }), {});
  const imported = ["executed", "executed_with_errors"].includes(job.status);
  const rolledBack = job.status === "rolled_back";
  const shown = records.filter((r) => filter === "all" || r.status === filter);

  return (
    <section className="view">
      <div className="view-head">
        <h1>{rolledBack ? "Import rolled back" : imported ? "Imported into HubSpot" : "Import into HubSpot"}</h1>
        <p className="lede">
          {rolledBack && "Contacts this import created were archived and the ones it updated were put back, except any changed in HubSpot since."}
          {imported && job.status === "executed" && "Every row went where the preview said it would."}
          {job.status === "executed_with_errors" && "Most rows went in. The ones below marked failed or not imported need fixing in the file; running the import again only retries those."}
          {job.status === "mapped" && "Contacts are matched by email: existing ones are updated, the rest are created. You can roll the whole import back afterwards."}
        </p>

        {busy && <p className="banner" aria-live="polite">{busy}…</p>}
        {error && (
          <div className="banner banner-error" role="alert">
            <p>{error.message}</p>
            {error.details.length > 0 && <ul>{error.details.map((d) => <li key={d}>{d}</li>)}</ul>}
          </div>
        )}

        {!busy && !rolledBack && config.hubspot_configured && (
          <div className="view-actions">
            {confirming === null && (
              <>
                {job.status !== "executed" && (
                  <button className="btn btn-primary" onClick={() => setConfirming("import")}>
                    {imported ? "Retry the rows that didn't import" : "Import into HubSpot"}
                  </button>
                )}
                {imported && <button className="btn btn-danger-quiet" onClick={() => setConfirming("rollback")}>Roll back this import</button>}
              </>
            )}
            {confirming && (
              <div className="confirm" role="group" aria-label="Confirm">
                <p>
                  {confirming === "import"
                    ? "This writes to your HubSpot account."
                    : "This archives the contacts this import created and restores the ones it changed."}
                </p>
                <button className={`btn ${confirming === "import" ? "btn-primary" : "btn-danger"}`} onClick={() => void run(confirming)}>
                  {confirming === "import" ? "Yes, import" : "Yes, roll back"}
                </button>
                <button className="btn" onClick={() => setConfirming(null)}>Cancel</button>
              </div>
            )}
          </div>
        )}
      </div>

      {records.length > 0 && (
        <>
          <div className="segmented" role="group" aria-label="Filter rows">
            <button aria-pressed={filter === "all"} onClick={() => setFilter("all")}>All {records.length}</button>
            {Object.entries(counts).map(([s, n]) => (
              <button key={s} aria-pressed={filter === s} onClick={() => setFilter(s)}>{RECORD_TEXT[s] ?? s} {n}</button>
            ))}
          </div>
          <div className="table-scroll">
            <table className="table records-table">
              <thead><tr><th>Row</th><th>Result</th><th>Email</th><th>HubSpot ID</th><th>Changed</th></tr></thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.row} className={`record-${r.status}`}>
                    <td className="num">{r.row + 1}</td>
                    <td><span className="pill">{RECORD_TEXT[r.status] ?? r.status}</span>{r.error && <p className="row-error">{r.error}</p>}</td>
                    <td>{r.email ?? <span className="blank">none</span>}</td>
                    <td className="num">{r.hubspot_id ?? ""}</td>
                    <td><div className="fields">
                      {r.before
                        ? Object.entries(r.payload).filter(([k]) => k !== "email" && (r.before?.[k] ?? "") !== r.payload[k]).map(([k, v]) => (
                          <span key={k} className="field"><span className="field-name">{k}</span> {r.before?.[k] || "empty"} to {v}</span>
                        ))
                        : ["created", "rolled_back"].includes(r.status) && Object.entries(r.payload).filter(([k]) => k !== "email").map(([k, v]) => (
                          <span key={k} className="field"><span className="field-name">{k}</span> {v}</span>
                        ))}
                    </div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

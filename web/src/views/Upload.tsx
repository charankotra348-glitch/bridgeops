import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Config, type Job, type JobSummary } from "../api";

const PROVIDER_HELP: Record<string, string> = {
  rules: "Rules only. Anything they aren't sure of comes to you.",
  gemini: "Rules first, then Gemini for the judgment calls.",
  jev: "Rules first, then Jev for the judgment calls.",
};

const STATUS_TEXT: Record<string, string> = {
  mapped: "In review",
  executed: "Imported",
  executed_with_errors: "Imported, some rows failed",
  rolled_back: "Rolled back",
  executing: "Importing",
  rolling_back: "Rolling back",
};

export function UploadView({ config, onCreated, onOpen }: {
  config: Config;
  onCreated: (job: Job) => void;
  onOpen: (id: string) => void;
}) {
  const [provider, setProvider] = useState(
    config.providers.includes(config.default_provider) ? config.default_provider : "rules");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { api.jobs().then(setJobs).catch(() => setJobs([])); }, []);

  async function start(file: File) {
    setError(null);
    try {
      setBusy(`Reading ${file.name}`);
      const source = await api.upload(file);
      setBusy(`Mapping ${source.columns.length} columns and checking for duplicates`);
      onCreated(await api.createJob(source.id, provider));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="view">
      <h1>Bring a customer export into HubSpot</h1>
      <p className="lede">
        Drop a CSV or JSON export. BridgeOps maps its columns to HubSpot contact fields, cleans up
        values like "Active-ish", and flags rows that look like the same person. You review only
        what it isn't sure about, then preview the import before anything is written.
      </p>

      <fieldset className="provider-pick">
        <legend>Who makes the judgment calls</legend>
        {["rules", "gemini", "jev"].map((p) => {
          const available = config.providers.includes(p);
          return (
            <label key={p} className={`provider-option${available ? "" : " is-unavailable"}`}>
              <input type="radio" name="provider" value={p} checked={provider === p} disabled={!available}
                onChange={() => setProvider(p)} />
              <span className="provider-name">{p === "rules" ? "Rules" : p === "gemini" ? "Gemini" : "Jev"}</span>
              <span className="provider-help">
                {available ? PROVIDER_HELP[p] : `Not set up: add the ${p === "gemini" ? "GEMINI_API_KEY" : "TYPESAFE_API_KEY"} to .env`}
              </span>
            </label>
          );
        })}
      </fieldset>

      <div
        className={`dropzone${dragging ? " is-dragging" : ""}${busy ? " is-busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragging(false);
          const f = e.dataTransfer.files[0];
          if (f && !busy) void start(f);
        }}
      >
        {busy ? <p aria-live="polite">{busy}…</p> : (
          <>
            <p>Drop the export here, or</p>
            <button className="btn btn-primary" onClick={() => input.current?.click()}>Choose a file</button>
            <p className="hint">CSV, TSV or JSON, up to 25 MB</p>
          </>
        )}
        <input ref={input} type="file" accept=".csv,.tsv,.json,.txt" hidden
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void start(f); e.target.value = ""; }} />
      </div>
      {error && <p className="banner banner-error" role="alert">{error}</p>}

      {jobs.length > 0 && (
        <>
          <h2>Earlier imports</h2>
          <table className="table jobs-table">
            <thead><tr><th>File</th><th>Rows</th><th>Decisions by</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td><button className="link" onClick={() => onOpen(j.id)}>{j.filename}</button></td>
                  <td className="num">{j.rows}</td>
                  <td>{j.provider === "rules" ? "Rules" : j.provider.charAt(0).toUpperCase() + j.provider.slice(1)}</td>
                  <td>{STATUS_TEXT[j.status] ?? j.status}</td>
                  <td>{new Date(j.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

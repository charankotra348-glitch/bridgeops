import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Config, type DuplicateGroup, type Job, type Source, type Target } from "./api";
import { outstanding, STAGES, stageOf, type Stage } from "./review";
import { DryRunView } from "./views/DryRun";
import { ImportView } from "./views/Import";
import { ReviewView } from "./views/Review";
import { UploadView } from "./views/Upload";

// The open job lives in the URL (#/jobs/<id>) so a reload or a shared link reopens it.
const jobIdFromHash = () => window.location.hash.match(/^#\/jobs\/(\w+)/)?.[1] ?? null;

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [targets, setTargets] = useState<Target[]>([]);
  const [jobId, setJobId] = useState<string | null>(jobIdFromHash);
  const [job, setJob] = useState<Job | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [duplicates, setDuplicates] = useState<DuplicateGroup[]>([]);
  const [stage, setStage] = useState<Stage>("upload");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.config(), api.targets()])
      .then(([c, t]) => { setConfig(c); setTargets(t); })
      .catch((e: ApiError) => setError(e.message));
    const onHash = () => setJobId(jobIdFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const refresh = useCallback(async (id: string, moveToNaturalStage = false) => {
    try {
      const j = await api.job(id);
      const [s, d] = await Promise.all([api.source(j.source_id), api.duplicates(id)]);
      setJob(j); setSource(s); setDuplicates(d); setError(null);
      if (moveToNaturalStage) setStage(stageOf(j, d));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (jobId) void refresh(jobId, true);
    else { setJob(null); setSource(null); setDuplicates([]); setStage("upload"); }
  }, [jobId, refresh]);

  const openJob = (id: string) => { window.location.hash = `#/jobs/${id}`; };
  const left = job ? outstanding(job, duplicates) : null;
  const natural = stageOf(job, duplicates);

  return (
    <div className="shell">
      <header className="masthead">
        <a className="wordmark" href="#/" onClick={() => setStage("upload")}>BridgeOps</a>
        {source && <span className="masthead-file">{source.filename}, {source.row_count} rows</span>}
      </header>

      <nav className="rail" aria-label="Import stages">
        <ol>
          {STAGES.map((s, i) => {
            const locked = (!job && s.id !== "upload") ||
              (s.id === "import" && natural !== "import" && (left?.total ?? 1) > 0);
            const done = STAGES.findIndex((x) => x.id === natural) > i;
            return (
              <li key={s.id}>
                <button
                  className={`rail-step${stage === s.id ? " is-current" : ""}${done ? " is-done" : ""}`}
                  aria-current={stage === s.id ? "step" : undefined}
                  disabled={locked}
                  onClick={() => setStage(s.id)}
                >
                  <span className="rail-num">{i + 1}</span>
                  <span className="rail-label">{s.label}</span>
                  {s.id === "review" && left && left.total > 0 && (
                    <span className="rail-badge">{left.total} need you</span>
                  )}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      <main className="stage">
        {error && <p className="banner banner-error" role="alert">{error}</p>}
        {stage === "upload" && config && (
          <UploadView config={config} onCreated={(j) => openJob(j.id)} onOpen={openJob} />
        )}
        {stage === "review" && job && source && (
          <ReviewView job={job} source={source} duplicates={duplicates} targets={targets}
            onChanged={() => refresh(job.id)} onDone={() => setStage("dryrun")} />
        )}
        {stage === "dryrun" && job && config && (
          <DryRunView job={job} config={config} onBackToReview={() => setStage("review")}
            onImport={() => setStage("import")} />
        )}
        {stage === "import" && job && config && (
          <ImportView job={job} config={config} onChanged={() => refresh(job.id)} />
        )}
      </main>
    </div>
  );
}

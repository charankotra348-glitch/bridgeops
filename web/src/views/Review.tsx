import { useState } from "react";
import { api, ApiError, type DuplicateGroup, type Job, type Mapping, type Source, type Target } from "../api";
import { decidedBy, needsReview, outstanding, pendingValues, percent } from "../review";

const SKIP = "__skip__";
const PICK = "__pick__";

/** Why a column is waiting on the reviewer, in their terms. */
export function explain(m: Mapping): string {
  const taken = m.reason.match(/also matched (\w+), already taken by '([^']+)'/);
  if (taken) return `This also looks like ${taken[1]}, but the column ${taken[2]} already fills it. Pick another field, or skip this one.`;
  const typeClash = m.reason.match(/name suggests (\w+) but values look like (\w+)/);
  if (typeClash) return `The name suggests ${typeClash[1]}, but the values look like ${typeClash[2]}. Pick the field they really belong in, or skip it.`;
  if (m.method === "none") return "BridgeOps didn't recognize this column. Pick the HubSpot field it belongs in, or skip it.";
  if (m.reason.includes("already mapped")) {
    return `${decidedBy(m.method)} picked a field another column already fills. Check which column should win.`;
  }
  return `${decidedBy(m.method)} suggests this field but is only ${percent(m.confidence)} sure, below the bar for deciding alone.`;
}

export function ReviewView({ job, source, duplicates, targets, onChanged, onDone }: {
  job: Job;
  source: Source;
  duplicates: DuplicateGroup[];
  targets: Target[];
  onChanged: () => Promise<void>;
  onDone: () => void;
}) {
  const left = outstanding(job, duplicates);
  const [showAll, setShowAll] = useState(left.total === 0);
  const [error, setError] = useState<string | null>(null);
  const locked = job.status !== "mapped";

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); await onChanged(); }
    catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
  };

  const columns = job.mappings.filter((m) => showAll || needsReview(m));
  const dups = duplicates.filter((d) => d.same_person !== false || d.status === "pending")
    .filter((d) => showAll || d.status === "pending");
  const profile = Object.fromEntries(source.columns.map((c) => [c.name, c]));

  return (
    <section className="view">
      <div className="view-head">
        <h1>{left.total === 0 ? "Everything is decided" : `${left.total} ${left.total === 1 ? "decision needs" : "decisions need"} you`}</h1>
        <p className="lede">
          {left.total === 0
            ? "Every column, value and duplicate has an answer. Preview the import next; nothing is written until you run it."
            : summary(left)}
        </p>
        <div className="view-actions">
          <div className="segmented" role="group" aria-label="Show">
            <button aria-pressed={!showAll} onClick={() => setShowAll(false)}>Needs you</button>
            <button aria-pressed={showAll} onClick={() => setShowAll(true)}>Everything</button>
          </div>
          <button className="btn btn-primary" disabled={left.total > 0} onClick={onDone}>Preview the import</button>
        </div>
        {locked && <p className="banner">This job has been imported, so its decisions can no longer change.</p>}
        {error && <p className="banner banner-error" role="alert">{error}</p>}
      </div>

      {columns.length > 0 && (
        <>
          <h2>Columns</h2>
          <p className="section-note">Left is the column in your file, right is the HubSpot field it goes into. The bar shows how sure the decision is and who made it.</p>
          <ul className="crossings">
            {columns.map((m) => (
              <MappingRow key={m.id} mapping={m} samples={profile[m.source_column]?.samples ?? []}
                targets={targets} locked={locked} act={act} />
            ))}
          </ul>
        </>
      )}

      {dups.length > 0 && (
        <>
          <h2>Possible duplicates</h2>
          <p className="section-note">HubSpot keeps one contact per email, so rows for the same person should become one record. Pick the row to keep; empty fields are filled from the others.</p>
          {dups.map((d) => <DuplicateCard key={d.id} group={d} locked={locked} act={act} />)}
        </>
      )}

      {columns.length === 0 && dups.length === 0 && (
        <p className="empty">Nothing waiting on you. Switch to Everything to look over what was decided automatically.</p>
      )}
    </section>
  );
}

function summary(left: { columns: number; values: number; duplicates: number }) {
  const parts = [];
  if (left.columns) parts.push(`${left.columns} ${left.columns === 1 ? "column" : "columns"} to place`);
  if (left.values) parts.push(`${left.values} ${left.values === 1 ? "value" : "values"} to translate`);
  if (left.duplicates) parts.push(`${left.duplicates} possible ${left.duplicates === 1 ? "duplicate" : "duplicates"} to settle`);
  return `${parts.join(", ")}. Everything else was decided automatically.`;
}

function Confidence({ method, confidence, status }: { method: string; confidence: number; status: string }) {
  const tone = status === "pending" ? "needs" : status === "rejected" ? "off" : "decided";
  const width = method === "none" ? 0 : Math.max(confidence, 0.04);
  return (
    <div className={`bar bar-${tone}`} title={method === "none" ? "No decision yet" : `${decidedBy(method)}, ${percent(confidence)} sure`}>
      <span className="bar-track"><span className="bar-fill" style={{ width: `${width * 100}%` }} /></span>
      <span className="bar-label">
        <span className="bar-who">{decidedBy(method)}</span>
        {method !== "none" && method !== "human" && <span className="bar-pct">{percent(confidence)}</span>}
      </span>
    </div>
  );
}

function MappingRow({ mapping: m, samples, targets, locked, act }: {
  mapping: Mapping;
  samples: string[];
  targets: Target[];
  locked: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const target = targets.find((t) => t.name === m.target_property);
  const undecided = m.status === "pending" && !m.target_property;
  const selectValue = undecided ? "" : m.status === "rejected" || !m.target_property ? SKIP : m.target_property;
  const pending = pendingValues(m);
  const [open, setOpen] = useState(pending.length > 0);

  const choose = (value: string) => act(() => value === SKIP
    ? api.updateMapping(m.id, { status: "rejected" })
    : api.updateMapping(m.id, { status: "approved", target_property: value }));

  return (
    <li className={`crossing${m.status === "pending" ? " is-pending" : ""}${m.status === "rejected" ? " is-skipped" : ""}`}>
      <div className="crossing-source">
        <span className="col-name">{m.source_column.trim() || <span className="blank">Column with no name</span>}</span>
        <span className="samples">{samples.slice(0, 4).map((s) => <code key={s}>{s}</code>)}</span>
      </div>

      <Confidence method={m.method} confidence={m.confidence} status={m.status} />

      <div className="crossing-target">
        <label className="sr-only" htmlFor={`t-${m.id}`}>HubSpot field for {m.source_column}</label>
        <select id={`t-${m.id}`} value={selectValue} disabled={locked} onChange={(e) => void choose(e.target.value)}>
          {undecided && <option value="" disabled>Choose a field…</option>}
          {targets.map((t) => <option key={t.name} value={t.name}>{t.label}</option>)}
          <option value={SKIP}>Don't import this column</option>
        </select>
        {m.status === "pending" && !undecided && !locked && (
          <button className="btn btn-small" onClick={() => void choose(selectValue)}>Accept</button>
        )}
      </div>

      {m.status === "pending" && <p className="crossing-reason">{explain(m)}</p>}

      {target?.type === "enum" && m.status !== "rejected" && Object.keys(m.value_map).length > 0 && (
        <div className="values">
          <button className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} how {Object.keys(m.value_map).length} values translate
            {pending.length > 0 && <span className="values-pending"> ({pending.length} need you)</span>}
          </button>
          {open && (
            <table className="table values-table">
              <thead><tr><th>In your file</th><th>Becomes</th><th>Decided by</th></tr></thead>
              <tbody>
                {Object.entries(m.value_map).map(([value, d]) => (
                  <tr key={value} className={d.status === "pending" ? "is-pending" : ""}>
                    <td><code>{value}</code></td>
                    <td>
                      <label className="sr-only" htmlFor={`v-${m.id}-${value}`}>HubSpot value for {value}</label>
                      <select id={`v-${m.id}-${value}`}
                        value={d.status === "pending" && !d.target ? PICK : d.target ?? ""} disabled={locked}
                        onChange={(e) => void act(() => api.updateMapping(m.id, {
                          status: "approved", value_map: { [value]: e.target.value || null } }))}>
                        {d.status === "pending" && !d.target && <option value={PICK} disabled>Choose a value…</option>}
                        <option value="">Leave empty</option>
                        {target.options.map((o) => <option key={o.value} value={o.value} title={o.description}>{o.value}</option>)}
                      </select>
                      {d.status === "pending" && d.target && !locked && (
                        <button className="btn btn-small" onClick={() => void act(() => api.updateMapping(m.id, {
                          status: "approved", value_map: { [value]: d.target } }))}>Accept</button>
                      )}
                    </td>
                    <td><Confidence method={d.method} confidence={d.confidence} status={d.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </li>
  );
}

function DuplicateCard({ group: g, locked, act }: {
  group: DuplicateGroup;
  locked: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [keep, setKeep] = useState<number>(g.keep_row ?? g.rows[0]);
  const fields = Array.from(new Set(g.records.flatMap((r) => Object.keys(r))));
  const verdict = g.status === "pending"
    ? (g.same_person === null ? "Needs your call" : `${decidedBy(g.method)} thinks ${g.same_person ? "same person" : "different people"}, ${percent(g.confidence)} sure`)
    : g.same_person ? `Same person${g.method === "human" ? ", decided by you" : `, ${g.reason}`}` : "Different people";

  return (
    <article className={`dup${g.status === "pending" ? " is-pending" : ""}`}>
      <header className="dup-head">
        <h3>Rows {g.rows.map((r) => r + 1).join(" and ")}</h3>
        <span className="dup-verdict">{verdict}</span>
      </header>
      <div className="table-scroll">
        <table className="table dup-table">
          <thead>
            <tr>
              <th>Keep</th>
              <th>Row</th>
              {fields.map((f) => <th key={f}>{f}</th>)}
            </tr>
          </thead>
          <tbody>
            {g.records.map((r, i) => {
              const row = g.rows[i];
              return (
                <tr key={row} className={keep === row ? "is-kept" : ""}>
                  <td>
                    <input type="radio" name={`keep-${g.id}`} aria-label={`Keep row ${row + 1}`}
                      checked={keep === row} disabled={locked || g.status !== "pending"}
                      onChange={() => setKeep(row)} />
                  </td>
                  <td className="num">{row + 1}</td>
                  {fields.map((f) => {
                    const differs = g.records.some((o) => (o[f] ?? "") !== (r[f] ?? ""));
                    return <td key={f} className={differs ? "differs" : ""}>{r[f] || <span className="blank">empty</span>}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {g.status === "pending" && !locked && (
        <div className="dup-actions">
          <button className="btn btn-primary" onClick={() => void act(() => api.updateDuplicate(g.id, { status: "approved", keep_row: keep }))}>
            Same person: keep row {keep + 1}
          </button>
          <button className="btn" onClick={() => void act(() => api.updateDuplicate(g.id, { status: "rejected" }))}>
            Different people: import both
          </button>
        </div>
      )}
    </article>
  );
}

// Typed client for the BridgeOps API. In development /api is proxied to FastAPI (vite.config.ts).

const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export type Status = "auto" | "pending" | "approved" | "rejected";

export interface ValueDecision {
  target: string | null;
  confidence: number;
  method: string;
  status: Status;
}

export interface Mapping {
  id: string;
  source_column: string;
  target_property: string | null;
  method: string; // rule | gemini | jev | human | none
  confidence: number;
  status: Status;
  reason: string;
  value_map: Record<string, ValueDecision>;
}

export interface Job {
  id: string;
  source_id: string;
  status: string; // mapped | executing | executed | executed_with_errors | rolling_back | rolled_back
  provider: string;
  counts: Record<string, unknown>;
  created_at: string;
  mappings: Mapping[];
}

export interface JobSummary {
  id: string;
  filename: string;
  rows: number;
  provider: string;
  status: string;
  created_at: string;
  counts: Record<string, unknown>;
}

export interface ColumnProfile {
  name: string;
  inferred_type: string;
  non_null: number;
  nulls: number;
  distinct: number;
  samples: string[];
  value_counts: Record<string, number>;
}

export interface Source {
  id: string;
  filename: string;
  row_count: number;
  columns: ColumnProfile[];
}

export interface Target {
  name: string;
  label: string;
  description: string;
  type: string;
  options: { value: string; description: string }[];
}

export interface DuplicateGroup {
  id: string;
  rows: number[];
  reason: string;
  method: string;
  confidence: number;
  same_person: boolean | null;
  status: Status;
  keep_row: number | null;
  records: Record<string, string>[];
}

export interface RowPlan {
  row: number;
  action: string;
  email: string | null;
  properties: Record<string, string>;
  errors: string[];
  merged_rows: number[];
  merged_into: number | null;
}

export interface Report {
  dry_run: boolean;
  blockers: string[];
  counts: Record<string, number>;
  records: RowPlan[];
  definitions: string;
  requests_sent: number;
  retries: number;
  reasons: { reason: string; rows: number; example: string; first_row: number }[];
}

export interface ImportRecord {
  row: number;
  email: string | null;
  status: string;
  hubspot_id: string | null;
  payload: Record<string, string>;
  before: Record<string, string | null> | null;
  error: string;
}

export interface Config {
  hubspot_configured: boolean;
  providers: string[];
  default_provider: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public details: string[] = []) {
    super(message);
  }
}

/** Fired when the API says the session is gone, so the app can show the sign-in screen. */
export const SIGNED_OUT = "bridgeops:signed-out";

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(BASE + path, {
      method,
      credentials: "include",
      headers: body instanceof FormData || body === undefined ? {} : { "content-type": "application/json" },
      body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the BridgeOps API. Is it running on port 8000?");
  }
  const data = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (resp.status === 401 && !path.startsWith("/auth/")) {
    window.dispatchEvent(new Event(SIGNED_OUT));
  }
  if (!resp.ok) {
    const detail = data?.detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      throw new ApiError(resp.status, detail.message, detail.blockers ?? []);
    }
    throw new ApiError(resp.status, typeof detail === "string" ? detail : `Request failed (${resp.status})`);
  }
  return data as T;
}

export const api = {
  me: () => call<{ email: string }>("GET", "/auth/me"),
  login: (email: string, password: string) => call<{ email: string }>("POST", "/auth/login", { email, password }),
  logout: () => call<null>("POST", "/auth/logout"),
  config: () => call<Config>("GET", "/config"),
  targets: () => call<Target[]>("GET", "/targets"),
  jobs: () => call<JobSummary[]>("GET", "/jobs"),
  job: (id: string) => call<Job>("GET", `/jobs/${id}`),
  source: (id: string) => call<Source>("GET", `/sources/${id}`),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return call<Source>("POST", "/sources", form);
  },
  createJob: (sourceId: string, provider: string) =>
    call<Job>("POST", "/jobs", { source_id: sourceId, provider }),
  updateMapping: (id: string, body: { status: "approved" | "rejected"; target_property?: string | null;
    value_map?: Record<string, string | null> }) =>
    call<Mapping>("PATCH", `/mappings/${id}`, body),
  duplicates: (jobId: string) => call<DuplicateGroup[]>("GET", `/jobs/${jobId}/duplicates`),
  updateDuplicate: (id: string, body: { status: "approved" | "rejected"; keep_row?: number | null }) =>
    call<DuplicateGroup>("PATCH", `/duplicates/${id}`, body),
  dryRun: (jobId: string) => call<Report>("POST", `/jobs/${jobId}/dry-run`),
  execute: (jobId: string) => call<Report>("POST", `/jobs/${jobId}/execute`),
  rollback: (jobId: string) => call<Report>("POST", `/jobs/${jobId}/rollback`),
  records: (jobId: string) => call<ImportRecord[]>("GET", `/jobs/${jobId}/records`),
};

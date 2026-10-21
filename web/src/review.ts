// Pure review logic: what still needs a person, and which stage a job is in.
import type { DuplicateGroup, Job, Mapping } from "./api";

export type Stage = "upload" | "review" | "dryrun" | "import";
export const STAGES: { id: Stage; label: string }[] = [
  { id: "upload", label: "Upload" },
  { id: "review", label: "Review" },
  { id: "dryrun", label: "Dry run" },
  { id: "import", label: "Import" },
];

export const pendingValues = (m: Mapping): string[] =>
  m.status === "rejected" ? [] : Object.entries(m.value_map).filter(([, d]) => d.status === "pending").map(([v]) => v);

export const needsReview = (m: Mapping): boolean => m.status === "pending" || pendingValues(m).length > 0;

export interface Outstanding {
  columns: number;
  values: number;
  duplicates: number;
  total: number;
}

export function outstanding(job: Job, duplicates: DuplicateGroup[]): Outstanding {
  const columns = job.mappings.filter((m) => m.status === "pending").length;
  const values = job.mappings.reduce((n, m) => n + pendingValues(m).length, 0);
  const dups = duplicates.filter((d) => d.status === "pending").length;
  return { columns, values, duplicates: dups, total: columns + values + dups };
}

const IMPORTED = new Set(["executing", "executed", "executed_with_errors", "rolling_back", "rolled_back"]);

/** The stage a job naturally sits in: review until nothing is pending, then dry run, then import. */
export function stageOf(job: Job | null, duplicates: DuplicateGroup[]): Stage {
  if (!job) return "upload";
  if (IMPORTED.has(job.status)) return "import";
  return outstanding(job, duplicates).total > 0 ? "review" : "dryrun";
}

/** Who made a decision, in the reviewer's words. */
export function decidedBy(method: string): string {
  switch (method) {
    case "rule":
      return "Rule";
    case "human":
      return "You";
    case "none":
      return "Nobody yet";
    case "missed":
      return "Missed";
    default:
      return method.charAt(0).toUpperCase() + method.slice(1); // Gemini, Jev
  }
}

export const percent = (x: number): string => `${Math.round(x * 100)}%`;

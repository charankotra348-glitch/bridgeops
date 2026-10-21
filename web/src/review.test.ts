import type { DuplicateGroup, Job, Mapping } from "./api";
import { fixFor } from "./views/DryRun";
import { decidedBy, needsReview, outstanding, pendingValues, stageOf } from "./review";

const mapping = (over: Partial<Mapping> = {}): Mapping => ({
  id: "m", source_column: "col", target_property: "email", method: "rule", confidence: 0.95,
  status: "auto", reason: "", value_map: {}, ...over,
});
const job = (mappings: Mapping[], status = "mapped"): Job => ({
  id: "j", source_id: "s", status, provider: "rules", counts: {}, created_at: "", mappings,
});
const dup = (status: DuplicateGroup["status"]): DuplicateGroup => ({
  id: "d", rows: [0, 1], reason: "", method: "none", confidence: 0, same_person: null, status, keep_row: null, records: [],
});

test("pending columns, values and duplicates all count as outstanding", () => {
  const stage = mapping({ id: "s", value_map: {
    Hot: { target: null, confidence: 0, method: "none", status: "pending" },
    Customer: { target: "customer", confidence: 0.98, method: "rule", status: "auto" },
  } });
  const j = job([mapping({ status: "pending" }), stage, mapping()]);
  expect(outstanding(j, [dup("pending"), dup("auto")])).toEqual({ columns: 1, values: 1, duplicates: 1, total: 3 });
  expect(pendingValues(stage)).toEqual(["Hot"]);
  expect(needsReview(stage)).toBe(true);
});

test("a rejected column's pending values don't block the import", () => {
  const skipped = mapping({ status: "rejected", value_map: {
    x: { target: null, confidence: 0, method: "none", status: "pending" } } });
  expect(outstanding(job([skipped]), []).total).toBe(0);
});

test("stage follows the job: review, then dry run, then import", () => {
  expect(stageOf(null, [])).toBe("upload");
  expect(stageOf(job([mapping({ status: "pending" })]), [])).toBe("review");
  expect(stageOf(job([mapping()]), [dup("pending")])).toBe("review");
  expect(stageOf(job([mapping()]), [])).toBe("dryrun");
  expect(stageOf(job([mapping()], "executed_with_errors"), [])).toBe("import");
});

test("decisions are attributed in plain words", () => {
  expect(decidedBy("human")).toBe("You");
  expect(decidedBy("gemini")).toBe("Gemini");
  expect(decidedBy("none")).toBe("Nobody yet");
});

test("each rejection reason comes with a fix", () => {
  expect(fixFor("no email: the import upserts by email, so every record needs one")).toMatch(/map the email column/);
  expect(fixFor("invalid email '…'")).toMatch(/doesn't hold email addresses/);
  expect(fixFor("'…' is not an option of '…' in this HubSpot account")).toMatch(/Translate this value/);
});

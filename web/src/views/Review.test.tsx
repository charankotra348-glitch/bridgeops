import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import type { DuplicateGroup, Job, Source, Target } from "../api";
import { explain, ReviewView } from "./Review";

const targets: Target[] = [
  { name: "email", label: "Email", description: "", type: "email", options: [] },
  { name: "lifecyclestage", label: "Lifecycle stage", description: "", type: "enum",
    options: [{ value: "lead", description: "" }, { value: "customer", description: "" }] },
];
const source: Source = { id: "s", filename: "x.csv", row_count: 2, columns: [
  { name: "cust_mail", inferred_type: "email", non_null: 2, nulls: 0, distinct: 2, samples: ["a@x.com"], value_counts: {} },
  { name: "status", inferred_type: "string", non_null: 2, nulls: 0, distinct: 2, samples: ["Hot"], value_counts: {} },
] };
const job: Job = { id: "j", source_id: "s", status: "mapped", provider: "rules", counts: {}, created_at: "", mappings: [
  { id: "m1", source_column: "cust_mail", target_property: "email", method: "rule", confidence: 0.97, status: "auto", reason: "", value_map: {} },
  { id: "m2", source_column: "status", target_property: "lifecyclestage", method: "rule", confidence: 0.9, status: "auto", reason: "",
    value_map: { Hot: { target: "lead", confidence: 0.6, method: "gemini", status: "pending" } } },
] };
const dup: DuplicateGroup = { id: "d1", rows: [0, 1], reason: "similar name", method: "none", confidence: 0,
  same_person: null, status: "pending", keep_row: null,
  records: [{ Email: "a@x.com", Name: "Li Wang" }, { Email: "b@y.com", Name: "Li Wang" }] };

function setup(dups = [dup]) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({}), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const onChanged = vi.fn(async () => {});
  render(<ReviewView job={job} source={source} duplicates={dups} targets={targets} onChanged={onChanged} onDone={() => {}} />);
  return { fetchMock, onChanged };
}

afterEach(() => vi.unstubAllGlobals());

test("shows only what needs a person, and blocks the preview until it's done", () => {
  setup();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("2 decisions need you");
  expect(screen.getByText("status")).toBeInTheDocument();
  expect(screen.queryByText("cust_mail")).not.toBeInTheDocument();   // decided automatically: hidden by default
  expect(screen.getByRole("button", { name: "Preview the import" })).toBeDisabled();
});

test("accepting a suggested value sends the reviewer's decision", async () => {
  const { fetchMock, onChanged } = setup();
  await userEvent.click(screen.getByRole("button", { name: "Accept" }));
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/mappings/m2");
  expect(JSON.parse(init.body as string)).toEqual({ status: "approved", value_map: { Hot: "lead" } });
  expect(onChanged).toHaveBeenCalled();
});

test("a duplicate is settled with the row the reviewer picks", async () => {
  const { fetchMock } = setup();
  await userEvent.click(screen.getByRole("radio", { name: "Keep row 2" }));
  await userEvent.click(screen.getByRole("button", { name: "Same person: keep row 2" }));
  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/duplicates/d1");
  expect(JSON.parse(init.body as string)).toMatchObject({ status: "approved", keep_row: 1 });
});

test("with nothing pending, the preview opens up", async () => {
  vi.stubGlobal("fetch", vi.fn());
  const onDone = vi.fn();
  const decided: Job = { ...job, mappings: [job.mappings[0], { ...job.mappings[1],
    value_map: { Hot: { target: "lead", confidence: 1, method: "human", status: "approved" } } }] };
  render(<ReviewView job={decided} source={source} duplicates={[]} targets={targets}
    onChanged={async () => {}} onDone={onDone} />);
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Everything is decided");
  await userEvent.click(screen.getByRole("button", { name: "Preview the import" }));
  expect(onDone).toHaveBeenCalled();
});


test("reasons are explained in the reviewer's terms", () => {
  const base = job.mappings[0];
  expect(explain({ ...base, method: "none", reason: "also matched email, already taken by 'Email'; no model decision, needs review" }))
    .toBe("This also looks like email, but the column Email already fills it. Pick another field, or skip this one.");
  expect(explain({ ...base, method: "none", reason: "no rule matched; no model decision, needs review" }))
    .toMatch(/didn't recognize this column/);
  expect(explain({ ...base, method: "gemini", confidence: 0.62, reason: "model chose firstname" }))
    .toBe("Gemini suggests this field but is only 62% sure, below the bar for deciding alone.");
});

test("an unrecognized column asks for a field instead of defaulting to skip", () => {
  vi.stubGlobal("fetch", vi.fn());
  const pending: Job = { ...job, mappings: [{ id: "m3", source_column: "fnm", target_property: null, method: "none",
    confidence: 0, status: "pending", reason: "no rule matched; no model decision, needs review", value_map: {} }] };
  render(<ReviewView job={pending} source={source} duplicates={[]} targets={targets} onChanged={async () => {}} onDone={() => {}} />);
  expect(screen.getByLabelText("HubSpot field for fnm")).toHaveDisplayValue("Choose a field…");
  expect(screen.queryByRole("button", { name: /Accept|don't import/ })).not.toBeInTheDocument();
});

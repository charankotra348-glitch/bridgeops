import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { SignInView } from "./SignIn";

afterEach(() => vi.unstubAllGlobals());

test("signs in and hands the email back", async () => {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ email: "charan@example.com" }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const onSignedIn = vi.fn();
  render(<SignInView onSignedIn={onSignedIn} />);
  await userEvent.type(screen.getByLabelText("Email"), "charan@example.com");
  await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("/api/auth/login");
  expect(init.credentials).toBe("include");
  expect(onSignedIn).toHaveBeenCalledWith("charan@example.com");
});

test("a wrong password shows the server's message and clears the field", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(
    JSON.stringify({ detail: "That email and password don't match an account" }), { status: 401 })));
  render(<SignInView onSignedIn={vi.fn()} />);
  await userEvent.type(screen.getByLabelText("Email"), "charan@example.com");
  await userEvent.type(screen.getByLabelText("Password"), "wrong one!");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("don't match an account");
  expect(screen.getByLabelText("Password")).toHaveValue("");
});

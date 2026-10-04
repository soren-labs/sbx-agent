import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate, authReturnPath } from "../hosted/AuthGate";
import { ApiKeys } from "../hosted/ApiKeys";
import { HostedReviewActions } from "../hosted/ReviewActions";
import { hostedRequest } from "../hosted/api";
import type { Session } from "../api/types";

vi.mock("../hosted/api", () => ({ hostedRequest: vi.fn(), hostedMode: false }));
const request = vi.mocked(hostedRequest);
function Destination() { return <p>Destination {useLocation().pathname}</p>; }
beforeEach(() => { request.mockReset(); });

describe("REV-006 auth destinations", () => {
  it("redirects a successful /auth login to the workspace", async () => {
    request.mockRejectedValueOnce(new Error("signed out")).mockResolvedValue({user:{id:"user"}});
    render(<MemoryRouter initialEntries={["/auth"]}><AuthGate><Destination /></AuthGate></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Email"), {target:{value:"test@example.test"}});
    fireEvent.change(screen.getByLabelText("Password"), {target:{value:"REDACTED"}});
    fireEvent.click(screen.getByRole("button", {name:"Sign in"}));
    expect(await screen.findByText("Destination /")).toBeVisible();
  });
  it("redirects signed-in /auth to a safe return path", async () => {
    request.mockResolvedValue({user:{id:"user"}});
    render(<MemoryRouter initialEntries={["/auth?returnTo=%2Fsessions%2Fsess_test"]}><AuthGate><Destination /></AuthGate></MemoryRouter>);
    expect(await screen.findByText("Destination /sessions/sess_test")).toBeVisible();
  });
  it("preserves direct protected-route login", async () => {
    request.mockRejectedValueOnce(new Error("signed out")).mockResolvedValue({user:{id:"user"}});
    render(<MemoryRouter initialEntries={["/sessions/sess_test"]}><AuthGate><Destination /></AuthGate></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Email"), {target:{value:"test@example.test"}});
    fireEvent.change(screen.getByLabelText("Password"), {target:{value:"REDACTED"}});
    fireEvent.click(screen.getByRole("button", {name:"Sign in"}));
    expect(await screen.findByText("Destination /sessions/sess_test")).toBeVisible();
  });
  it.each(["https://foreign.test", "//foreign.test", "/\\foreign.test", "/auth", "/missing", "/%2f%2fforeign.test"])("rejects unsafe return path %s", path => {
    expect(authReturnPath("?returnTo=" + encodeURIComponent(path))).toBe("/");
  });
});

it("REV-007 API key form supports keyboard creation and revocation", async () => {
  const key = {id:"key-test",label:"mobile",scopes:["agents"]};
  request.mockImplementation(async (path, body) => {
    if (path.endsWith("/key-test")) return {};
    if (body) return {key:"REDACTED"};
    return {api_keys:[key]};
  });
  render(<ApiKeys />);
  const user = userEvent.setup();
  await user.click(screen.getByLabelText("Key name"));
  await user.type(screen.getByLabelText("Key name"), "mobile");
  await user.tab();
  expect(screen.getByLabelText("Key expiry")).toHaveFocus();
  await user.selectOptions(screen.getByLabelText("Key expiry"), "30");
  await user.tab();
  expect(screen.getByRole("button", {name:"Create API key"})).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(await screen.findByLabelText("New API key")).toHaveValue("REDACTED");
  await user.click(screen.getByRole("button", {name:"Dismiss key"}));
  await user.click(screen.getByRole("button", {name:"Revoke mobile"}));
  await waitFor(() => expect(request).toHaveBeenCalledWith("/hosted/api-keys/key-test", {}, "DELETE"));
});

it("REV-009 displays a terminal failed review and offers a new attempt", async () => {
  request.mockResolvedValue({sessions:[{reviewer_session_id:"sess_failed",status:"failed",error:"provider_exhausted"}]});
  render(<MemoryRouter><HostedReviewActions session={{id:"sess_author"} as Session} /></MemoryRouter>);
  expect(await screen.findByText("Review failed — retry with a new review Session")).toBeVisible();
  expect(screen.queryByText("Review running")).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name:"Launch independent review"})).toBeEnabled();
});

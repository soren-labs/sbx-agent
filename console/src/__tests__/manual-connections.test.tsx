import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { hostedRequest } from "../hosted/api";
import { HostedIntegrations } from "../hosted/Integrations";
import { ManualConnection } from "../hosted/ManualConnection";
import { PrototypeApp } from "../prototype/PrototypeApp";
import { ApiProvider } from "../state/api";
import { HttpSessionApi } from "../api/http";

vi.mock("../hosted/api", () => ({ hostedMode: true, hostedRequest: vi.fn() }));
const request = vi.mocked(hostedRequest);
beforeEach(() => { request.mockReset(); localStorage.clear(); });

it("shows the minimum manual credentials with optional Codex and GitHub App", async () => {
  request.mockResolvedValue({ connection: null, configured: true, mock: false, installations: [] });
  render(<HostedIntegrations />);
  expect(await screen.findByLabelText("OpenCode Zen API Key")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("GitHub Token")).toHaveAttribute("type", "password");
  expect(screen.getByLabelText("Modal Token ID")).toBeVisible();
  expect(screen.getByLabelText("Modal Token Secret")).toBeVisible();
  expect(screen.getByText("Optional ChatGPT / Codex").closest("details")).not.toHaveAttribute("open");
  expect(screen.getByText("Optional GitHub App connection").closest("details")).not.toHaveAttribute("open");
  expect(screen.getByRole("button", {name: "Connect OpenCode Zen"})).toBeVisible();
});

it.each([
  { provider: "github" as const, title: "GitHub", field: "token" as const, label: "GitHub Token" },
  { provider: "opencode" as const, title: "OpenCode Zen", field: "api_key" as const, label: "OpenCode Zen API Key" },
])("connects, replaces, validates and disconnects $title without browser persistence", async props => {
  let state: string | null = null;
  request.mockImplementation(async (_path, body, method) => {
    if (method === "DELETE") state = "disabled";
    else if (body) state = "connected";
    return { connection: state ? { id: "connection", state, metadata: {} } : null };
  });
  const storage = vi.spyOn(Storage.prototype, "setItem");
  render(<ManualConnection {...props} help="Manual token" />);
  const card = within(screen.getByRole("region", {name: `${props.title} connection`}));
  fireEvent.change(card.getByLabelText(props.label), {target: {value: "REDACTED"}});
  fireEvent.click(card.getByRole("button", {name: `Connect ${props.title}`}));
  expect(card.getByLabelText(props.label)).toHaveValue("");
  expect(await card.findByText("Connected")).toBeVisible();
  expect(request).toHaveBeenCalledWith(`/hosted/connections/${props.provider}`, {[props.field]: "REDACTED"});
  fireEvent.change(card.getByLabelText(props.label), {target: {value: "REDACTED"}});
  fireEvent.click(card.getByRole("button", {name: `Replace ${props.title}`}));
  await waitFor(() => expect(card.getByRole("button", {name: `Validate ${props.title}`})).toBeEnabled());
  fireEvent.click(card.getByRole("button", {name: `Validate ${props.title}`}));
  await waitFor(() => expect(request).toHaveBeenCalledWith(`/hosted/connections/${props.provider}/validate`, {}));
  await waitFor(() => expect(card.getByRole("button", {name: `Disconnect ${props.title}`})).toBeEnabled());
  fireEvent.click(card.getByRole("button", {name: `Disconnect ${props.title}`}));
  expect(await card.findByText("Disabled")).toBeVisible();
  expect(storage).not.toHaveBeenCalled();
  storage.mockRestore();
});

it("shows an invalid connection with actionable validation failure", async () => {
  request.mockImplementation(async (_path, body) => {
    if (body) throw new Error("github token expired replace token");
    return {connection:{state:"invalid",metadata:{}}};
  });
  render(<ManualConnection provider="github" title="GitHub" field="token" label="GitHub Token" help="Manual" />);
  expect(await screen.findByText("Invalid")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"Validate GitHub"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("replace token");
});

it("composes a Session with Zen and a manual GitHub repository without Codex", async () => {
  request.mockResolvedValue({repositories:[{name:"owner/repo"}]});
  const api = new HttpSessionApi();
  vi.spyOn(api,"listSessions").mockResolvedValue([]);
  vi.spyOn(api,"listProviders").mockResolvedValue([{id:"opencode",label:"OpenCode",available:true}] as any);
  vi.spyOn(api,"listModels").mockResolvedValue([{provider:"opencode",model:"opencode/free",account:"zen"}] as any);
  const create = vi.spyOn(api,"createSession").mockRejectedValue(new Error("capture submission"));
  render(<ApiProvider client={api}><MemoryRouter><PrototypeApp /></MemoryRouter></ApiProvider>);
  fireEvent.change(screen.getByLabelText("Session task"), {target:{value:"Build hello"}});
  await waitFor(() => expect(screen.getByRole("button", {name:"Start session"})).toBeEnabled());
  fireEvent.click(screen.getByTitle("Select repository"));
  fireEvent.click(await screen.findByText("owner/repo"));
  fireEvent.click(screen.getByRole("button", {name:"Start session"}));
  await waitFor(() => expect(create).toHaveBeenCalledWith(expect.objectContaining({provider:"opencode",model:"auto",repo:"owner/repo"})));
});

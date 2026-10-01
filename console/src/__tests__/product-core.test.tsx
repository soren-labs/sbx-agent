import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { PrototypeApp } from "../prototype/PrototypeApp";
import { ApiProvider } from "../state/api";
import { HttpSessionApi } from "../api/http";
import { createApi } from "../api";
function open(client: HttpSessionApi) {
  return render(<ApiProvider client={client}><MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><PrototypeApp /></MemoryRouter></ApiProvider>);
}
describe("live product shell", () => {
  it("uses HTTP unless mock mode is explicit", () => {expect(createApi()).toBeInstanceOf(HttpSessionApi);});
  it("shows a failed list request and recovers through Try again", async () => {
    const client = new HttpSessionApi();
    const list = vi.spyOn(client, "listSessions").mockRejectedValueOnce(new Error("Connection unavailable")).mockResolvedValue([]);
    vi.spyOn(client, "listProviders").mockResolvedValue([]);
    vi.spyOn(client, "listModels").mockResolvedValue([]);
    open(client);
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection unavailable");
    fireEvent.click(screen.getByText("Try again"));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(list).toHaveBeenCalledTimes(2);
  });
  it("retains a rejected task with automatic effort and no delivery by default", async () => {
    const client = new HttpSessionApi();
    vi.spyOn(client, "listSessions").mockResolvedValue([]);
    vi.spyOn(client, "listProviders").mockResolvedValue([]);
    vi.spyOn(client, "listModels").mockResolvedValue([]);
    const create = vi.spyOn(client, "createSession").mockRejectedValue(new Error("Repository unavailable"));
    open(client);
    fireEvent.change(screen.getByLabelText("Session task"), {target:{value:"Read the API"}});
    await waitFor(() => expect(screen.getByRole("button", {name:"Start session"})).toBeEnabled());
    fireEvent.click(screen.getByRole("button", {name:"Start session"}));
    expect(await screen.findByText("Repository unavailable")).toBeVisible();
    expect(screen.getByLabelText("Session task")).toHaveValue("Read the API");
    expect(create).toHaveBeenCalledWith(expect.objectContaining({prompt:"Read the API",provider:"auto",model:"auto",effort:"auto",delivery:"none"}));
  });
});

import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "../components/Composer";
import { PROVIDERS } from "../api/fixtures";
import { makeApi, renderApp } from "../test/helpers";
import type { NewSessionInput } from "../api/types";

const setup = (onSubmit = vi.fn()) =>
  renderApp(
    <Composer providers={PROVIDERS} submitting={false} onSubmit={onSubmit} />,
  );

describe("Composer", () => {
  it("renders prompt, repo, provider/model Auto selects, and Send", () => {
    setup();
    expect(screen.getByTestId("composer-prompt")).toBeInTheDocument();
    expect(screen.getByLabelText("Repository")).toBeInTheDocument();
    expect(screen.getByLabelText("Provider")).toHaveValue("auto");
    expect(screen.getByLabelText("Model")).toHaveValue("auto");
    expect(screen.getByTestId("composer-send")).toBeInTheDocument();
  });

  it("keeps advanced fields collapsed behind the disclosure", async () => {
    setup();
    const adv = screen.getByTestId("advanced");
    expect(adv).not.toHaveAttribute("open");
    expect(adv).not.toBeVisible();
    await userEvent.click(screen.getByText("Advanced"));
    expect(screen.getByTestId("advanced")).toHaveAttribute("open");
    expect(screen.getByLabelText("Effort")).toBeInTheDocument();
    expect(screen.getByLabelText("Delivery")).toBeInTheDocument();
    expect(screen.getByLabelText(/Idle timeout/)).toBeInTheDocument();
    expect(screen.getByLabelText(/MCP servers/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Secrets/)).toBeInTheDocument();
  });

  it("requires a prompt before submitting", async () => {
    const onSubmit = vi.fn();
    setup(onSubmit);
    await userEvent.click(screen.getByTestId("composer-send"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("prompt");
  });

  it("submits prompt + repo + provider/model selections", async () => {
    const onSubmit = vi.fn();
    setup(onSubmit);
    await userEvent.type(
      screen.getByTestId("composer-prompt"),
      "Fix the flaky test",
    );
    await userEvent.type(screen.getByLabelText("Repository"), "a/b");
    await userEvent.selectOptions(screen.getByLabelText("Provider"), "grok");
    await userEvent.selectOptions(screen.getByLabelText("Model"), "grok-4");
    await userEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    const input = onSubmit.mock.calls[0][0] as NewSessionInput;
    expect(input).toMatchObject({
      prompt: "Fix the flaky test",
      repo: "a/b",
      provider: "grok",
      model: "grok-4",
    });
  });

  it("Auto provider leaves provider as auto and model undefined", async () => {
    const onSubmit = vi.fn();
    setup(onSubmit);
    await userEvent.type(screen.getByTestId("composer-prompt"), "x");
    await userEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    const input = onSubmit.mock.calls[0][0] as NewSessionInput;
    expect(input.provider).toBe("auto");
    expect(input.model).toBeUndefined();
  });

  it("passes advanced fields through when set", async () => {
    const onSubmit = vi.fn();
    setup(onSubmit);
    await userEvent.type(screen.getByTestId("composer-prompt"), "x");
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.selectOptions(screen.getByLabelText("Effort"), "high");
    await userEvent.selectOptions(screen.getByLabelText("Delivery"), "draft_pr");
    await userEvent.type(screen.getByLabelText(/Idle timeout/), "900");
    await userEvent.type(
      screen.getByLabelText(/Secrets/),
      "AWS_KEY, GH_TOKEN",
    );
    await userEvent.click(screen.getByTestId("composer-send"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    const input = onSubmit.mock.calls[0][0] as NewSessionInput;
    expect(input).toMatchObject({
      effort: "high",
      delivery: "draft_pr",
      idleTimeoutS: 900,
      secrets: ["AWS_KEY", "GH_TOKEN"],
    });
  });

  it("surfaces provider_busy as an actionable notice", async () => {
    const api = makeApi("provider_busy");
    const onSubmit = vi.fn(async () => {
      await api.createSession({ prompt: "x" });
    });
    setup(onSubmit);
    await userEvent.type(screen.getByTestId("composer-prompt"), "x");
    await userEvent.click(screen.getByTestId("composer-send"));
    const notice = await screen.findByTestId("error-notice");
    expect(notice).toHaveAttribute("data-kind", "provider_busy");
    expect(notice).toHaveTextContent("busy");
  });

  it("never renders raw account/scheduler internals", () => {
    setup();
    const html = document.body.innerHTML;
    expect(html).not.toMatch(/lru|scheduler candidate|modal-[a-z0-9]+/i);
  });
});

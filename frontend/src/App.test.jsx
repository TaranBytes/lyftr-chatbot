import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";

const okBody = (overrides = {}) => ({
  session_id: "ignored",
  reply: "The capital of France is Paris.",
  metadata: {
    resolution_source: "LIVE_INFERENCE", model: "gemini/gemini-2.5-flash", latency_ms: 812,
    input_tokens: 42, output_tokens: 9, key_id: "key_1", fallback_attempts: 0, ...overrides,
  },
});
const reply = (status, body) => Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => body });

let fetchMock;
beforeEach(() => { fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => vi.unstubAllGlobals());

async function sendMessage(text) {
  const user = userEvent.setup();
  await user.type(screen.getByTestId("chat-input"), text);
  await user.click(screen.getByTestId("send-button"));
  return user;
}

describe("required test ids", () => {
  it("exposes chat-input, send-button and system-prompt-input before anything is sent", () => {
    render(<App />);
    expect(screen.getByTestId("chat-input")).toBeInTheDocument();
    expect(screen.getByTestId("send-button")).toBeInTheDocument();
    expect(screen.getByTestId("system-prompt-input")).toBeInTheDocument();
  });

  it("renders every assistant reply with all five metadata elements and the exact contents", async () => {
    fetchMock.mockReturnValue(reply(200, okBody()));
    render(<App />);
    await sendMessage("What is the capital of France?");

    const msg = await screen.findByTestId("assistant-message");
    expect(msg).toHaveTextContent("The capital of France is Paris.");
    expect(within(msg).getByTestId("meta-source")).toHaveTextContent(/^LIVE_INFERENCE$/);
    expect(within(msg).getByTestId("meta-model")).toHaveTextContent(/^gemini\/gemini-2.5-flash$/);
    expect(within(msg).getByTestId("meta-latency")).toHaveTextContent(/^812 ms$/);
    expect(within(msg).getByTestId("meta-input-tokens")).toHaveTextContent(/^42$/);
    expect(within(msg).getByTestId("meta-output-tokens")).toHaveTextContent(/^9$/);
  });

  it("shows LLM_CACHE exactly on a cache hit", async () => {
    fetchMock.mockReturnValue(reply(200, okBody({ resolution_source: "LLM_CACHE", key_id: null, latency_ms: 3 })));
    render(<App />);
    await sendMessage("hi");
    expect(await screen.findByTestId("meta-source")).toHaveTextContent(/^LLM_CACHE$/);
    expect(screen.getByTestId("meta-latency")).toHaveTextContent(/^3 ms$/);
  });

  it("each assistant reply gets its own metadata (multi-turn)", async () => {
    fetchMock
      .mockReturnValueOnce(reply(200, okBody({ input_tokens: 10 })))
      .mockReturnValueOnce(reply(200, okBody({ input_tokens: 25 })));
    render(<App />);
    const user = await sendMessage("one");
    await screen.findAllByTestId("assistant-message");
    await user.type(screen.getByTestId("chat-input"), "two");
    await user.click(screen.getByTestId("send-button"));
    await waitFor(() => expect(screen.getAllByTestId("assistant-message")).toHaveLength(2));
    const tokens = screen.getAllByTestId("meta-input-tokens").map((n) => n.textContent);
    expect(tokens).toEqual(["10", "25"]);
  });
});

describe("sending", () => {
  it("clears chat-input after sending and posts the contract body", async () => {
    fetchMock.mockReturnValue(reply(200, okBody()));
    render(<App />);
    await sendMessage("hello there");
    expect(screen.getByTestId("chat-input")).toHaveValue("");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/chat$/);
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(Object.keys(body).sort()).toEqual(["message", "session_id"]); // no system_prompt unless typed
    expect(body.message).toBe("hello there");
    expect(body.session_id.length).toBeGreaterThan(0);
  });

  it("sends with Enter, but Shift+Enter does not send", async () => {
    fetchMock.mockReturnValue(reply(200, okBody()));
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("chat-input"), "line one{Shift>}{Enter}{/Shift}line two");
    expect(fetchMock).not.toHaveBeenCalled();
    await user.type(screen.getByTestId("chat-input"), "{Enter}");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).message).toBe("line one\nline two");
  });

  it("does not send an empty / whitespace-only message", async () => {
    render(<App />);
    const user = userEvent.setup();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    await user.type(screen.getByTestId("chat-input"), "   {Enter}");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("includes the system prompt only when one is typed", async () => {
    fetchMock.mockReturnValue(reply(200, okBody()));
    render(<App />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("system-prompt-input"), "Talk like a pirate");
    await user.type(screen.getByTestId("chat-input"), "hi{Enter}");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).system_prompt).toBe("Talk like a pirate");
  });

  it("shows a loading indicator and disables sending while waiting", async () => {
    let resolve;
    fetchMock.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<App />);
    await sendMessage("slow one");
    expect(screen.getByTestId("loading-indicator")).toBeInTheDocument();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    resolve({ ok: true, status: 200, json: async () => okBody() });
    await screen.findByTestId("assistant-message");
    expect(screen.queryByTestId("loading-indicator")).not.toBeInTheDocument();
  });
});

describe("sessions", () => {
  it("uses the same session id within a page and a new one on each page load", async () => {
    fetchMock.mockReturnValue(reply(200, okBody()));
    const first = render(<App />);
    const user = await sendMessage("a");
    await screen.findByTestId("assistant-message");
    await user.type(screen.getByTestId("chat-input"), "b{Enter}");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const ids = fetchMock.mock.calls.map((c) => JSON.parse(c[1].body).session_id);
    expect(ids[0]).toBe(ids[1]);

    first.unmount();
    render(<App />); // "page load" #2
    await sendMessage("c");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).session_id).not.toBe(ids[0]);
  });
});

describe("errors are readable and shown inside the chat", () => {
  it("422 validation error", async () => {
    fetchMock.mockReturnValue(reply(422, { detail: [{ msg: "Value error, message must be 1-4000 characters after trimming whitespace" }] }));
    render(<App />);
    await sendMessage("x");
    const err = await screen.findByTestId("error-message");
    expect(err).toHaveTextContent(/couldn't be sent: message must be 1-4000 characters/);
    expect(screen.queryByTestId("assistant-message")).not.toBeInTheDocument();
  });

  it("503 all providers unavailable", async () => {
    fetchMock.mockReturnValue(reply(503, { detail: "all providers unavailable" }));
    render(<App />);
    await sendMessage("x");
    expect(await screen.findByTestId("error-message")).toHaveTextContent(/All AI providers are unavailable/);
    expect(screen.queryByTestId("assistant-message")).not.toBeInTheDocument();
  });

  it("network failure", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<App />);
    await sendMessage("x");
    expect(await screen.findByTestId("error-message")).toHaveTextContent(/Can't reach the server/);
  });

  it("server returns HTML / garbage: still no crash, still a readable message", async () => {
    fetchMock.mockReturnValue(Promise.resolve({ ok: false, status: 502, json: async () => { throw new Error("not json"); } }));
    render(<App />);
    await sendMessage("x");
    expect(await screen.findByTestId("error-message")).toHaveTextContent(/Something went wrong on the server \(status 502\)/);
  });

  it("the user can send again after an error", async () => {
    fetchMock.mockReturnValueOnce(reply(503, { detail: "all providers unavailable" })).mockReturnValueOnce(reply(200, okBody()));
    render(<App />);
    const user = await sendMessage("first");
    await screen.findByTestId("error-message");
    await user.type(screen.getByTestId("chat-input"), "second{Enter}");
    expect(await screen.findByTestId("assistant-message")).toBeInTheDocument();
  });
});

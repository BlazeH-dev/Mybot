import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TracePanel } from "@/components/thread/TracePanel";

describe("TracePanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({
        available: true,
        source: "local",
        session_key: "websocket:chat-1",
        turn_id: "turn-1",
        trace_id: "trace-1",
        trace_url: null,
        usage: { input_tokens: 12, output_tokens: 4, total_tokens: 16 },
        spans: [{
          span_id: "span-1",
          parent_span_id: null,
          actor: "main",
          name: "mybot.agent.run",
          started_at: "2026-08-11T00:00:00Z",
          ended_at: "2026-08-11T00:00:01Z",
          duration_ms: 1000,
          status: "completed",
          attributes: {},
          events: [{
            timestamp: "2026-08-11T00:00:00Z",
            name: "gen_ai.agent.run.start",
            attributes: { "mybot.iteration": 1 },
          }],
        }],
      }),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads and renders the current turn trace", async () => {
    render(
      <TracePanel
        sessionKey="websocket:chat-1"
        token="tok"
        turnId="turn-1"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "View current turn trace" }));

    await waitFor(() => expect(screen.getByText("mybot.agent.run")).toBeInTheDocument());
    expect(screen.getByText("16")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/trace?turn_id=turn-1",
      expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
    );

    fireEvent.click(screen.getByText("mybot.agent.run"));
    expect(screen.getByText("gen_ai.agent.run.start")).toBeInTheDocument();
  });
});

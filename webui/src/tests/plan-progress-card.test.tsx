import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  latestPlanSnapshot,
  PlanProgressCard,
} from "@/components/thread/PlanProgressCard";
import type { UIMessage } from "@/lib/types";

function planMessage(status = "awaiting_confirmation"): UIMessage {
  return {
    id: "plan-trace",
    role: "tool",
    kind: "trace",
    content: "plan()",
    traces: ["plan()"],
    createdAt: 1,
    toolEvents: [{
      version: 1,
      phase: "end",
      call_id: "call-plan",
      name: "plan",
      arguments: { action: "create" },
      result: JSON.stringify({
        path: "/tmp/.nanobot-runtime/artifacts/task_ui/plan.json",
        plan: {
          task_id: "task_ui",
          goal: "Implement plan mode",
          status,
          plan_hash: "abc123",
          steps: [
            { id: "inspect", description: "Inspect the current flow", status: "done" },
            { id: "build", description: "Build the UI", status: "in_progress" },
            { id: "verify", description: "Run tests", status: "pending" },
          ],
        },
      }),
    }],
  };
}

describe("PlanProgressCard", () => {
  it("parses plan tool results and renders step progress", () => {
    const plan = latestPlanSnapshot([planMessage()]);
    expect(plan).toMatchObject({
      taskId: "task_ui",
      status: "awaiting_confirmation",
      planHash: "abc123",
    });

    render(<PlanProgressCard plan={plan!} />);

    expect(screen.getByText("Implement plan mode")).toBeInTheDocument();
    expect(screen.getByText("Inspect the current flow")).toBeInTheDocument();
    expect(screen.getByText("Build the UI")).toBeInTheDocument();
    expect(screen.getByText("Run tests")).toBeInTheDocument();
  });

  it("executes an awaiting plan once from the action button", () => {
    const onExecute = vi.fn();
    const plan = latestPlanSnapshot([planMessage()]);
    render(<PlanProgressCard plan={plan!} onExecute={onExecute} />);

    fireEvent.click(screen.getByRole("button", { name: "Execute plan" }));

    expect(onExecute).toHaveBeenCalledWith("task_ui", "abc123");
    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
  });
});

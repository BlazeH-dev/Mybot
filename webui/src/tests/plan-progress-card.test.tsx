import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  latestPlanSnapshot,
  PlanProgressCard,
} from "@/components/thread/PlanProgressCard";
import type { SubagentActivity, UIMessage } from "@/lib/types";

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
          revision: 2,
          plan_markdown: {
            path: "/tmp/.nanobot-runtime/artifacts/task_ui/plan.md",
          },
          steps: [
            { id: "inspect", description: "Inspect the current flow", status: "succeeded" },
            {
              id: "build",
              description: "Build the UI",
              status: "running",
              executor: "child",
              child_id: "child-1",
            },
            {
              id: "verify",
              description: "Run tests",
              status: "pending",
              depends_on: ["inspect", "build"],
            },
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

    expect(onExecute).toHaveBeenCalledWith("task_ui", "abc123", "confirm");
    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
  });

  it("offers resume for an active plan even when its latest card snapshot is running", () => {
    const onExecute = vi.fn();
    const plan = latestPlanSnapshot([planMessage("active")]);
    const { rerender } = render(<PlanProgressCard plan={plan!} onExecute={onExecute} />);

    fireEvent.click(screen.getByRole("button", { name: "Resume plan" }));

    expect(onExecute).toHaveBeenCalledWith("task_ui", "abc123", "resume");
    expect(screen.getByRole("button", { name: "Resuming…" })).toBeDisabled();

    rerender(
      <PlanProgressCard
        plan={{ ...plan!, updatedAt: "2026-08-11T00:00:00+08:00" }}
        onExecute={onExecute}
      />,
    );
    expect(screen.getByRole("button", { name: "Resume plan" })).toBeEnabled();
  });

  it("opens the current Markdown plan and renders DAG metadata", () => {
    const onOpenPlan = vi.fn();
    const plan = latestPlanSnapshot([planMessage()]);
    render(<PlanProgressCard plan={plan!} onOpenPlan={onOpenPlan} />);

    expect(screen.getByText("r2 · abc123")).toBeInTheDocument();
    expect(screen.getByText("child")).toBeInTheDocument();
    expect(screen.getByText("Depends on: inspect, build")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View full plan" }));
    expect(onOpenPlan).toHaveBeenCalledWith(
      "/tmp/.nanobot-runtime/artifacts/task_ui/plan.md",
    );
  });

  it("collapses to a one-line plan bar and can expand again", () => {
    const onCollapsedChange = vi.fn();
    const plan = latestPlanSnapshot([planMessage()]);
    const { rerender } = render(
      <PlanProgressCard plan={plan!} onCollapsedChange={onCollapsedChange} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Hide plan" }));
    expect(onCollapsedChange).toHaveBeenCalledWith(true);

    rerender(
      <PlanProgressCard
        plan={plan!}
        collapsed
        onCollapsedChange={onCollapsedChange}
      />,
    );
    expect(screen.queryByText("Inspect the current flow")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show plan" }));
    expect(onCollapsedChange).toHaveBeenLastCalledWith(false);
  });

  it("opens current-revision subagent activity without rendering stale children", () => {
    const plan = latestPlanSnapshot([planMessage("active")]);
    const activities: SubagentActivity[] = [
      {
        childId: "child-1",
        label: "Build worker",
        taskDescription: "Implement the desktop plan card",
        parentTaskId: "task_ui",
        planHash: "abc123",
        nodeId: "build",
        status: "running",
        phase: "awaiting_tools",
        iteration: 1,
        elapsedMs: 4200,
        reasoning: "Inspecting the component tree.",
        reasoningStreaming: true,
        toolEvents: [{ call_id: "call-1", name: "read_file", phase: "start" }],
        usage: { prompt_tokens: 12 },
      },
      {
        childId: "stale-child",
        label: "Stale worker",
        taskDescription: "Old revision",
        parentTaskId: "task_ui",
        planHash: "old-hash",
        nodeId: "build",
        status: "completed",
        phase: "done",
        iteration: 0,
        elapsedMs: 100,
        reasoning: "stale reasoning",
        reasoningStreaming: false,
        toolEvents: [],
        usage: {},
      },
      {
        childId: "unbound-child",
        label: "Unbound worker",
        taskDescription: "Legacy activity without a revision binding",
        parentTaskId: "task_ui",
        nodeId: "build",
        status: "completed",
        phase: "done",
        iteration: 0,
        elapsedMs: 100,
        reasoning: "unbound reasoning",
        reasoningStreaming: false,
        toolEvents: [],
        usage: {},
      },
    ];
    render(<PlanProgressCard plan={plan!} subagentActivities={activities} />);

    expect(plan!.steps.find((step) => step.id === "build")?.childId).toBe("child-1");
    fireEvent.click(screen.getByRole("button", { name: "1 subagents running" }));

    expect(screen.getByRole("dialog", { name: "Subagent execution" })).toBeInTheDocument();
    expect(screen.getByText("Inspecting the component tree.")).toBeInTheDocument();
    expect(screen.getByText("read_file")).toBeInTheDocument();
    expect(screen.queryByText("Stale worker")).not.toBeInTheDocument();
    expect(screen.queryByText("Unbound worker")).not.toBeInTheDocument();
  });
});

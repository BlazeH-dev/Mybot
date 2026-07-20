import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InteractionRequests } from "@/components/thread/InteractionRequests";

describe("InteractionRequests", () => {
  it("renders a parameter-bound approval and submits approve once", () => {
    const onRespond = vi.fn();
    render(
      <InteractionRequests
        interactions={[{
          request_id: "ir_1",
          revision: 2,
          kind: "approval",
          strategy: "expire_and_deny",
          status: "pending",
          created_at: "2026-07-18T00:00:00+00:00",
          payload: {
            binding: {
              reason: "High-risk command requires approval",
              target: "git commit -m test",
            },
          },
        }]}
        onRespond={onRespond}
      />,
    );
    expect(screen.getByText("High-risk command requires approval")).toBeInTheDocument();
    expect(screen.getByText("git commit -m test")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve once" }));
    expect(onRespond).toHaveBeenCalledWith("ir_1", 2, { approved: true });
  });

  it("keeps plan confirmation out of the generic interaction card list", () => {
    const onRespond = vi.fn();
    render(
      <InteractionRequests
        interactions={[{
          request_id: "ir_plan",
          revision: 1,
          kind: "plan_confirmation",
          strategy: "required",
          status: "pending",
          created_at: "2026-07-18T00:00:00+00:00",
          payload: { goal: "Build report" },
        }]}
        onRespond={onRespond}
      />,
    );
    expect(screen.queryByTestId("interaction-requests")).not.toBeInTheDocument();
    expect(screen.queryByText("Build report")).not.toBeInTheDocument();
    expect(onRespond).not.toHaveBeenCalled();
  });

  it("renders typed single, multiple, and free-text questions", () => {
    const onRespond = vi.fn();
    render(
      <InteractionRequests
        interactions={[{
          request_id: "ir_questions",
          revision: 3,
          kind: "question",
          strategy: "required",
          status: "pending",
          created_at: "2026-07-18T00:00:00+00:00",
          payload: {},
          questions: [
            {
              id: "format",
              header: "Format",
              question: "Choose a format",
              options: [{ label: "Markdown", description: "Plain text" }],
            },
            {
              id: "sections",
              header: "Sections",
              question: "Choose sections",
              multiple: true,
              options: [
                { label: "Summary", description: "Add summary" },
                { label: "Risks", description: "Add risks" },
              ],
            },
            {
              id: "title",
              header: "Title",
              question: "Enter a title",
            },
          ],
        }]}
        onRespond={onRespond}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Markdown" }));
    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    fireEvent.click(screen.getByRole("button", { name: "Risks" }));
    fireEvent.change(screen.getByPlaceholderText("Type your answer"), {
      target: { value: "Weekly report" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onRespond).toHaveBeenCalledWith("ir_questions", 3, {
      answers: {
        format: "Markdown",
        sections: ["Summary", "Risks"],
        title: "Weekly report",
      },
    });
  });
});

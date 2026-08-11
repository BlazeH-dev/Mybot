import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InteractionRequests } from "@/components/thread/InteractionRequests";
import { setAppLanguage } from "@/i18n";

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

  it("localizes approval chrome and known policy reasons", async () => {
    await setAppLanguage("zh-CN");
    render(
      <InteractionRequests
        interactions={[{
          request_id: "ir_localized_approval",
          revision: 1,
          kind: "approval",
          strategy: "expire_and_deny",
          status: "pending",
          created_at: "2026-08-11T00:00:00+00:00",
          payload: {
            reason_i18n_key: "thread.interaction.approvalReason.highRiskCommand",
            binding: {
              reason: "high-risk local command requires approval in Default Permission",
              target: "git commit -m test",
            },
          },
        }]}
        onRespond={vi.fn()}
      />,
    );
    expect(screen.getByText("需要批准")).toBeInTheDocument();
    expect(screen.getByText("默认权限下，执行高风险本地命令需要批准。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "仅本次批准" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeInTheDocument();
  });

  it("keeps plan confirmation and removed reflection requests out of the card list", () => {
    const onRespond = vi.fn();
    render(
      <InteractionRequests
        interactions={[
          {
            request_id: "ir_plan",
            revision: 1,
            kind: "plan_confirmation",
            strategy: "required",
            status: "pending",
            created_at: "2026-07-18T00:00:00+00:00",
            payload: { goal: "Build report" },
          },
          {
            request_id: "ir_legacy_reflection",
            revision: 1,
            kind: "reflection_decision",
            strategy: "required",
            status: "pending",
            created_at: "2026-08-10T00:00:00+00:00",
            payload: {},
          },
        ]}
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

  it("localizes runtime HITL cards while preserving protocol answer values", async () => {
    await setAppLanguage("zh-CN");
    const onRespond = vi.fn();
    render(
      <InteractionRequests
        interactions={[{
          request_id: "ir_recovery",
          revision: 1,
          kind: "recovery_decision",
          strategy: "required",
          status: "pending",
          created_at: "2026-08-10T00:00:00+00:00",
          payload: {},
          questions: [{
            id: "recovery_action",
            header: "Recovery",
            header_i18n_key: "thread.interaction.recovery.header",
            question: "Choose the next recovery action",
            question_i18n_key: "thread.interaction.recovery.question",
            options: [
              {
                label: "Retry",
                label_i18n_key: "thread.interaction.recovery.retry",
                description: "Retry after checking",
                description_i18n_key: "thread.interaction.recovery.retryDescription",
              },
            ],
          }],
        }]}
        onRespond={onRespond}
      />,
    );
    expect(screen.getByText("需要你的输入")).toBeInTheDocument();
    expect(screen.getByText("恢复执行")).toBeInTheDocument();
    expect(screen.getByText(/中断任务的执行结果不确定/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    expect(onRespond).toHaveBeenCalledWith("ir_recovery", 1, {
      answers: { recovery_action: "Retry" },
      answer: "Retry",
    });
  });
});

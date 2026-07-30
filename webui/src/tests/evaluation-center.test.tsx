import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvaluationCenter } from "@/components/evaluations/EvaluationCenter";
import {
  deleteEvaluationRun,
  fetchEvaluationCatalog,
  fetchEvaluationCases,
  fetchEvaluationReadiness,
  fetchEvaluationRuns,
} from "@/lib/api";
import type { EvaluationRequestPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", () => ({
  deleteEvaluationRun: vi.fn(),
  fetchEvaluationCatalog: vi.fn(),
  fetchEvaluationReadiness: vi.fn(),
  fetchEvaluationRuns: vi.fn(),
  fetchEvaluationCases: vi.fn().mockResolvedValue([]),
}));

const startEvaluation = vi.fn();
const resumeEvaluation = vi.fn();
const evaluationHandlers = new Set<(event: never) => void>();
const client = {
  onEvaluation: (handler: (event: never) => void) => {
    evaluationHandlers.add(handler);
    return () => evaluationHandlers.delete(handler);
  },
  startEvaluation,
  cancelEvaluation: vi.fn(),
  retryEvaluation: vi.fn(),
  resumeEvaluation,
};

function renderCenter() {
  render(
    <ClientProvider client={client as never} token="token">
      <EvaluationCenter />
    </ClientProvider>,
  );
}

describe("EvaluationCenter", () => {
  beforeEach(() => {
    startEvaluation.mockReset().mockReturnValue("request-1");
    resumeEvaluation.mockReset().mockReturnValue("request-resume-1");
    vi.mocked(deleteEvaluationRun).mockReset().mockResolvedValue({ deleted: true });
    vi.mocked(fetchEvaluationCases).mockReset().mockResolvedValue([]);
    evaluationHandlers.clear();
    vi.mocked(fetchEvaluationCatalog).mockResolvedValue({
      schema_version: 1,
      suites: [{
        id: "office",
        version: "1.0.0",
        label: "Office benchmark",
        description: "Office",
        profiles: [
          { id: "ci", label: "CI" },
          { id: "office-smoke", label: "Office smoke" },
          { id: "office-release", label: "Office release" },
        ],
        benchmarks: [
          { id: "ocb", label: "OCB" },
        ],
        skills: [{ id: "officecli", label: "officecli", available: true, compatible: true }],
        model_presets: [{ id: "gpt-5-6-luna", label: "gpt-5-6-luna" }],
        runtime_profiles: [{ id: "default", label: "Default runtime" }],
        benchmark_samples: {
          ocb: [255, 509, 1018],
        },
      }],
    });
    vi.mocked(fetchEvaluationReadiness).mockImplementation(async (_token, request: EvaluationRequestPayload) => ({
      ready: request.profile === "ci",
      blockers: request.profile === "ci" ? [] : ["licensed content required"],
      warnings: [],
      checks: {},
      estimate: {
        skill_runs: request.profile === "ci" ? 0 : 24,
        judge_runs: request.profile === "ci" ? 0 : 16,
        estimated_tokens: { total: request.profile === "ci" ? 0 : 768000 },
      },
    }));
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-running",
        suite_id: "office",
        profile: "office-smoke",
        status: "running",
        phase: "running",
        total_cases: 24,
        completed_cases: 6,
      }],
      langfuse: { available: true, runs: [] },
    });
  });

  it("shows all profile readiness, queue progress, and starts ready CI", async () => {
    renderCenter();

    expect(await screen.findByRole("heading", { name: "Evaluation center" })).toBeInTheDocument();
    expect(screen.getByText("office-release")).toBeInTheDocument();
    expect(await screen.findByText("6/24")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Profile"), { target: { value: "ci" } });
    const startButton = await screen.findByRole("button", { name: "Start evaluation" });
    await waitFor(() => expect(startButton).toBeEnabled());
    fireEvent.click(startButton);

    expect(startEvaluation).toHaveBeenCalledWith(expect.objectContaining({
      profile: "ci",
      action: "run",
    }));
  });

  it("selects an OCB release sample size", async () => {
    renderCenter();

    fireEvent.change(await screen.findByLabelText("Profile"), {
      target: { value: "office-release" },
    });
    fireEvent.change(await screen.findByLabelText("OCB sample"), {
      target: { value: "255" },
    });
    await waitFor(() => expect(fetchEvaluationReadiness).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({
        profile: "office-release",
        benchmark_samples: {
          ocb: 255,
        },
      }),
    ));
  });

  it("resumes unfinished cases on the same interrupted job", async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-interrupted",
        suite_id: "office",
        profile: "office-smoke",
        status: "interrupted",
        phase: "interrupted",
        total_cases: 24,
        completed_cases: 9,
        remaining_cases: 15,
        resumed_cases: 9,
        resumable: true,
        resume_count: 1,
      }],
      langfuse: { available: true, runs: [] },
    });
    renderCenter();

    const resumeButton = await screen.findByTitle("Resume unfinished cases");
    expect(screen.getByText(/9\/24.*15 remaining.*9 reused.*resumed 1x/)).toBeInTheDocument();
    fireEvent.click(resumeButton);

    expect(resumeEvaluation).toHaveBeenCalledWith("job-interrupted");
  });

  it("shows actual usage and performance metrics for a Langfuse run", async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [],
      langfuse: {
        available: true,
        runs: [{
          source: "langfuse",
          job_id: null,
          dataset_run_id: "run-usage",
          dataset_name: "mybot-office-smoke",
          name: "office-smoke",
          status: "completed",
          item_count: 1,
          completed_items: 1,
          failed_items: 0,
          aggregate_scores: {
            mybot_score: 0.875,
            output: "This corrected model output is intentionally much longer than a score summary should ever render in the evaluation history table.",
          },
          usage: {
            input_tokens: 1200,
            output_tokens: 300,
            total_tokens: 1500,
          },
          metrics: {
            generation_count: 4,
            latency_seconds: 12.4,
            ttft_seconds: 0.8,
          },
          review_status: "not_required",
          created_at: "2026-07-29T00:00:00Z",
        }],
      },
    });
    renderCenter();

    expect(await screen.findByText("1,500")).toBeInTheDocument();
    expect(screen.getByText("in 1,200 · out 300")).toBeInTheDocument();
    expect(screen.getByText("4 calls · 12s · TTFT 0.8s")).toBeInTheDocument();
    expect(screen.queryByText(/This corrected model output/)).not.toBeInTheDocument();
  });

  it("groups multiple Langfuse links and deletes a local history job after confirmation", async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-failed",
        suite_id: "office",
        profile: "office-smoke",
        status: "failed",
        phase: "failed",
        failure: {
          category: "evaluator_error",
          label: "Evaluator code error",
          summary: "The evaluator callback failed, so required benchmark scores were not produced.",
          detail: "unexpected keyword argument 'input'",
          retryable: true,
          signals: [{
            category: "model_relay_unavailable",
            label: "Model relay HTTP 503",
            summary: "The model relay returned a temporary service error.",
            count: 7,
          }],
        },
        langfuse_links: ["https://langfuse.test/run-1", "https://langfuse.test/run-2"],
      }],
      langfuse: { available: true, runs: [] },
    });
    renderCenter();

    const reasonButton = await screen.findByRole("button", { name: "View failure reason" });
    expect(screen.queryByText("Evaluator code error")).not.toBeInTheDocument();
    fireEvent.click(reasonButton);
    expect(await screen.findByRole("heading", { name: "Evaluation failure details" })).toBeInTheDocument();
    expect(screen.getByText("Evaluator code error")).toBeInTheDocument();
    expect(screen.getByText("unexpected keyword argument 'input'")).toBeInTheDocument();
    expect(screen.getByText("Model relay HTTP 503")).toBeInTheDocument();
    expect(screen.getByText("7 occurrences")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Evaluation failure details" })).not.toBeInTheDocument());
    const linksButton = await screen.findByRole("button", { name: "Open 2 Langfuse runs" });
    expect(screen.queryByRole("link", { name: "Open Langfuse" })).not.toBeInTheDocument();
    fireEvent.pointerDown(linksButton, { button: 0 });
    expect(await screen.findByRole("menuitem", { name: "Langfuse run 1" })).toHaveAttribute(
      "href",
      "https://langfuse.test/run-1",
    );
    expect(screen.getByRole("menuitem", { name: "Langfuse run 2" })).toHaveAttribute(
      "href",
      "https://langfuse.test/run-2",
    );
    fireEvent.keyDown(document, { key: "Escape" });

    fireEvent.click(screen.getByRole("button", { name: "Delete evaluation history" }));
    expect(screen.getByText(/permanently delete its linked Langfuse Dataset Run data/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete history" }));

    await waitFor(() => expect(deleteEvaluationRun).toHaveBeenCalledWith("token", "job-failed"));
    await waitFor(() => expect(screen.queryByText("job-failed".slice(0, 12))).not.toBeInTheDocument());
  });

  it("shows only the compared models in the case Variant column", async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-model-comparison",
        suite_id: "office",
        profile: "office-smoke",
        status: "failed",
        phase: "failed",
        request: {
          suite_id: "office",
          profile: "office-smoke",
          action: "run",
          benchmarks: ["ocb"],
          skills: ["officecli"],
          model_presets: ["gpt-5-6-luna", "deepseek-v4-flash"],
          runtime_profiles: ["default"],
          benchmark_samples: { ocb: 255 },
          allow_licensed_content: true,
        },
      }],
      langfuse: { available: true, runs: [] },
    });
    vi.mocked(fetchEvaluationCases).mockResolvedValue([
      {
        case_id: "602",
        benchmark: "ocb",
        skill: "officecli",
        model_preset: "gpt-5-6-luna",
        status: "completed",
      },
      {
        case_id: "602",
        benchmark: "ocb",
        skill: "officecli",
        model_preset: "deepseek-v4-flash",
        status: "completed",
      },
    ]);
    renderCenter();

    fireEvent.click(await screen.findByRole("button", { name: "Toggle case details" }));

    expect(await screen.findByText("gpt-5.6-luna")).toBeInTheDocument();
    expect(screen.getByText("DeepSeek-v4-flash")).toBeInTheDocument();
    expect(screen.queryByText("ocb / officecli / gpt-5-6-luna")).not.toBeInTheDocument();
  });

  it("offers deletion for a completed remote Langfuse result", async () => {
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [],
      langfuse: {
        available: true,
        runs: [{
          source: "langfuse",
          job_id: null,
          dataset_run_id: "remote-completed",
          dataset_name: "mybot-office",
          name: "office-smoke completed",
          status: "completed",
          item_count: 1,
          completed_items: 1,
          failed_items: 0,
          aggregate_scores: { mybot_score: 0.9 },
          review_status: "not_required",
        }],
      },
    });
    renderCenter();

    fireEvent.click(await screen.findByRole("button", { name: "Delete evaluation history" }));
    expect(screen.getByText(/Permanently delete office-smoke completed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete history" }));

    await waitFor(() => expect(deleteEvaluationRun).toHaveBeenCalledWith("token", "remote-completed"));
    await waitFor(() => expect(screen.queryByText("office-smoke completed")).not.toBeInTheDocument());
  });

  it("shows the request failure inside the delete dialog", async () => {
    vi.mocked(deleteEvaluationRun).mockRejectedValueOnce(new Error("Failed to fetch"));
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-delete-error",
        suite_id: "office",
        profile: "office-smoke",
        status: "failed",
        phase: "failed",
      }],
      langfuse: { available: true, runs: [] },
    });
    renderCenter();

    fireEvent.click(await screen.findByRole("button", { name: "Delete evaluation history" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete history" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("删除失败：Failed to fetch");
    expect(screen.getByRole("heading", { name: "Delete evaluation history?" })).toBeInTheDocument();
  });

  it("removes history as soon as background deletion is scheduled", async () => {
    vi.mocked(deleteEvaluationRun).mockResolvedValueOnce({
      deleted: true,
      scheduled: true,
    });
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-local-deleted",
        suite_id: "office",
        profile: "office-smoke",
        status: "failed",
        phase: "failed",
      }],
      langfuse: { available: true, runs: [] },
    });
    renderCenter();

    fireEvent.click(await screen.findByRole("button", { name: "Delete evaluation history" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete history" }));

    await waitFor(() => expect(screen.queryByText("job-local-del")).not.toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Delete evaluation history?" })).not.toBeInTheDocument();
  });
});

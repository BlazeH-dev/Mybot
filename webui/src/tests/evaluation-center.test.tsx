import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvaluationCenter } from "@/components/evaluations/EvaluationCenter";
import {
  fetchEvaluationCatalog,
  fetchEvaluationReadiness,
  fetchEvaluationRuns,
} from "@/lib/api";
import type { EvaluationRequestPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", () => ({
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
          { id: "officebench", label: "OfficeBench" },
          { id: "presentbench", label: "PresentBench" },
        ],
        skills: [{ id: "officecli", label: "officecli", available: true, compatible: true }],
        model_presets: [{ id: "gpt-5-6-luna", label: "gpt-5-6-luna" }],
        runtime_profiles: [{ id: "default", label: "Default runtime" }],
        benchmark_samples: {
          ocb: [255, 509, 1018],
          officebench: [24, 47, 93],
          presentbench: [60, 119, 238],
        },
        presentbench_samples: [60, 119, 238],
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

  it("selects a release sample size for every benchmark", async () => {
    renderCenter();

    fireEvent.change(await screen.findByLabelText("Profile"), {
      target: { value: "office-release" },
    });
    fireEvent.change(await screen.findByLabelText("OCB sample"), {
      target: { value: "255" },
    });
    fireEvent.change(screen.getByLabelText("OfficeBench sample"), {
      target: { value: "24" },
    });

    await waitFor(() => expect(fetchEvaluationReadiness).toHaveBeenCalledWith(
      "token",
      expect.objectContaining({
        profile: "office-release",
        benchmark_samples: {
          ocb: 255,
          officebench: 24,
          presentbench: 238,
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
          aggregate_scores: { mybot_score: 0.875 },
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
  });
});

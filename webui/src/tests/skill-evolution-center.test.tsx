import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SkillEvolutionCenter } from "@/components/evaluations/SkillEvolutionCenter";
import {
  analyzeSkillEvolution,
  evolveSkillEvolution,
  fetchEvaluationRuns,
  fetchSettings,
  fetchSkillEvolutionActivities,
  fetchSkillEvolutionBadCases,
  fetchSkillEvolutionTask,
  runSkillEvolutionAction,
} from "@/lib/api";
import type { SkillEvolutionTask } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", () => ({
  analyzeSkillEvolution: vi.fn(),
  evolveSkillEvolution: vi.fn(),
  fetchEvaluationRuns: vi.fn(),
  fetchSettings: vi.fn(),
  fetchSkillEvolutionActivities: vi.fn(),
  fetchSkillEvolutionBadCases: vi.fn(),
  fetchSkillEvolutionTask: vi.fn(),
  runSkillEvolutionAction: vi.fn(),
}));

const client = {};

function analysisTask(): SkillEvolutionTask {
  return {
    schema_version: 2,
    task_id: "task-1",
    title: "OfficeCLI evaluation-driven evolution",
    source_run_id: "job-1",
    source_profile: "office-release",
    source_model_preset: "gpt-5-6-luna",
    base_skill: "officecli",
    derived_skill: "officecli-evolved",
    optimizer_model: "gpt-5-6-sol",
    threshold: 0.6,
    status: "analysis_ready",
    selected_cases: [],
    evidence_digest: "evidence-digest",
    active_analysis_id: "a1",
    analyses: [{
      analysis_id: "a1",
      evidence_digest: "evidence-digest",
      digest: "analysis-digest",
      summary: "Validation was skipped after editing the document.",
      findings: [
        {
          finding_id: "f1",
          case_ids: ["1"],
          root_cause: "Missing validation loop",
          fix_owner: "skill",
          confidence: 0.9,
          evidence_refs: ["ev-1"],
          symptoms: ["Invalid output"],
          skill_gap: "No retry after validation",
          change_hypothesis: "Require validation and repair before delivery.",
          expected_effect: "Fewer invalid artifacts",
          risk: "Extra tool calls",
          should_modify_skill: true,
        },
        {
          finding_id: "f2",
          case_ids: ["1"],
          root_cause: "Provider timeout",
          fix_owner: "provider",
          confidence: 0.8,
          evidence_refs: ["ev-1"],
          symptoms: [],
          skill_gap: "",
          change_hypothesis: "",
          expected_effect: "",
          risk: "",
          should_modify_skill: false,
        },
      ],
      clusters: [],
      usage: { total_tokens: 100 },
      batch_count: 1,
      created_at: "2026-01-01T00:00:00Z",
    }],
    revisions: [],
    active_revision_id: null,
    activity_cursor: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function renderPage() {
  render(
    <ClientProvider client={client as never} token="token">
      <SkillEvolutionCenter />
    </ClientProvider>,
  );
}

describe("SkillEvolutionCenter", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(fetchEvaluationRuns).mockResolvedValue({
      jobs: [{
        job_id: "job-1",
        suite_id: "office",
        profile: "office-release",
        action: "run",
        status: "awaiting_review",
        phase: "awaiting_review",
        request: {
          suite_id: "office",
          profile: "office-release",
          action: "run",
          benchmarks: ["ocb"],
          skills: ["officecli"],
          model_presets: ["gpt-5-6-luna"],
          runtime_profiles: ["default"],
          benchmark_samples: { ocb: 211 },
          allow_licensed_content: false,
        },
      }],
      langfuse: { available: true, runs: [] },
    });
    vi.mocked(fetchSettings).mockResolvedValue({
      model_presets: [
        { name: "gpt-5-6-sol", label: "GPT-5.6 Sol", active: false, is_default: false, model: "gpt-5.6-sol", provider: "openai", max_tokens: 8192, context_window_tokens: 262144, temperature: 0.1, reasoning_effort: null },
        { name: "gpt-5-6-luna", label: "GPT-5.6 Luna", active: true, is_default: false, model: "gpt-5.6-luna", provider: "openai", max_tokens: 8192, context_window_tokens: 262144, temperature: 0.1, reasoning_effort: null },
        { name: "deepseek-v4-flash", label: "DeepSeek V4 Flash", active: false, is_default: false, model: "deepseek-v4-flash", provider: "deepseek", max_tokens: 8192, context_window_tokens: 65536, temperature: 0.1, reasoning_effort: null },
      ],
      providers: [
        { name: "openai", label: "OpenAI", configured: true },
        { name: "deepseek", label: "DeepSeek", configured: true },
      ],
    } as never);
    vi.mocked(fetchSkillEvolutionBadCases).mockResolvedValue({
      threshold: 0.6,
      cases: [{
        case_key: ["ocb", "officecli", "gpt-5-6-luna", "1"].join("\0"),
        case_id: "1",
        benchmark: "ocb",
        skill: "officecli",
        model_preset: "gpt-5-6-luna",
        status: "completed",
        score: 0.25,
      }],
    });
    vi.mocked(analyzeSkillEvolution).mockResolvedValue(analysisTask());
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(analysisTask());
    vi.mocked(fetchSkillEvolutionActivities).mockResolvedValue({
      cursor: 1,
      activities: [{
        seq: 1,
        timestamp: "2026-01-01T00:00:00Z",
        phase: "analyzing",
        kind: "model",
        status: "completed",
        label: "Analyzed batch 1/1",
        usage: { total_tokens: 100 },
      }],
    });
    vi.mocked(evolveSkillEvolution).mockResolvedValue({ ...analysisTask(), status: "editing" });
    vi.mocked(runSkillEvolutionAction).mockResolvedValue(analysisTask());
  });

  it("analyzes Cases, shows findings, and disables non-Skill owners", async () => {
    renderPage();
    fireEvent.click(await screen.findByLabelText("Case 1"));
    fireEvent.click(screen.getByRole("button", { name: /Analyze 1 low-scoring Cases/ }));

    expect(await screen.findByText("Missing validation loop")).toBeInTheDocument();
    expect(screen.getByLabelText("Finding f1")).toBeChecked();
    expect(screen.getByLabelText("Finding f2")).toBeDisabled();
    expect(analyzeSkillEvolution).toHaveBeenCalledWith(
      "token", "job-1", 0.6, "gpt-5-6-luna", "gpt-5-6-sol", ["1"],
    );
  });

  it("starts restricted editing from selected findings", async () => {
    renderPage();
    fireEvent.click(await screen.findByLabelText("Case 1"));
    fireEvent.click(screen.getByRole("button", { name: /Analyze 1 low-scoring Cases/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Modify Skill from 1 findings/ }));

    await waitFor(() => expect(evolveSkillEvolution).toHaveBeenCalledWith(
      "token", "task-1", "a1", "analysis-digest", ["f1"],
    ));
  });

  it("selects every Case without a fixed count limit", async () => {
    vi.mocked(fetchSkillEvolutionBadCases).mockResolvedValue({
      threshold: 0.6,
      cases: Array.from({ length: 21 }, (_, index) => ({
        case_key: ["ocb", "officecli", "gpt-5-6-luna", String(index + 1)].join("\0"),
        case_id: String(index + 1), benchmark: "ocb", skill: "officecli",
        model_preset: "gpt-5-6-luna", status: "completed", score: 0.25,
      })),
    });
    renderPage();
    await screen.findByLabelText("Case 21");
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("21/21")).toBeInTheDocument();
    expect(screen.getByLabelText("Case 21")).toBeChecked();
  });

  it("separates evaluation models and submits only the active model", async () => {
    vi.mocked(fetchSkillEvolutionBadCases).mockResolvedValue({
      threshold: 0.6,
      cases: [
        { case_key: ["ocb", "officecli", "gpt-5-6-luna", "1"].join("\0"), case_id: "1", benchmark: "ocb", skill: "officecli", model_preset: "gpt-5-6-luna", status: "completed", score: 0.25 },
        { case_key: ["ocb", "officecli", "deepseek-v4-flash", "2"].join("\0"), case_id: "2", benchmark: "ocb", skill: "officecli", model_preset: "deepseek-v4-flash", status: "completed", score: 0.2 },
      ],
    });
    renderPage();
    fireEvent.click(await screen.findByRole("tab", { name: "DeepSeek V4 Flash · 1" }));
    fireEvent.click(screen.getByLabelText("Case 2"));
    fireEvent.change(screen.getByLabelText("Optimizer model"), { target: { value: "gpt-5-6-luna" } });
    fireEvent.click(screen.getByRole("button", { name: /Analyze 1 low-scoring Cases/ }));

    await waitFor(() => expect(analyzeSkillEvolution).toHaveBeenCalledWith(
      "token", "job-1", 0.6, "deepseek-v4-flash", "gpt-5-6-luna", ["2"],
    ));
  });
});

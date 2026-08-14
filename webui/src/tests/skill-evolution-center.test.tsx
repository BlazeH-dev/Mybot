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
  fetchSkillEvolutionTasks,
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
  fetchSkillEvolutionTasks: vi.fn(),
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
      categories: [
        {
          category_id: "c1",
          title: "Grounded completeness workflow",
          root_cause: "Missing validation loop",
          fix_owner: "skill",
          confidence: 0.9,
          finding_ids: ["f1"],
          case_ids: ["1"],
          risk: "Extra verification step",
          should_modify_skill: true,
          intervention: {
            repair_mode: "workflow_required",
            trigger: "A final answer must cover exact requested claims.",
            required_action: "Build and verify a claim-to-source checklist.",
            entrypoint: "SKILL.md",
            required_outputs: [],
            final_answer_check: ["Every requested claim is present."],
            observable_success: "The final answer covers every checklist item.",
          },
        },
        {
          category_id: "c2",
          title: "Provider timeout",
          root_cause: "Provider timeout",
          fix_owner: "provider",
          confidence: 0.8,
          finding_ids: ["f2"],
          case_ids: ["1"],
          risk: "Not controlled by the Skill",
          should_modify_skill: false,
          intervention: {
            repair_mode: "not_skill_repairable",
            trigger: "The provider times out.",
            required_action: "Escalate to provider/runtime diagnostics.",
            entrypoint: "provider",
            required_outputs: [],
            final_answer_check: ["Do not claim a Skill repair."],
            observable_success: "The category is excluded from editing.",
          },
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
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([]);
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

  it("restores the latest server task when the local task pointer is missing", async () => {
    const running = {
      ...analysisTask(),
      status: "collecting_evidence",
      active_analysis_id: null,
      analyses: [],
    } satisfies SkillEvolutionTask;
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([running]);
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(running);

    renderPage();

    expect(await screen.findByText("collecting_evidence")).toBeInTheDocument();
    expect(fetchSkillEvolutionTasks).toHaveBeenCalledWith("token");
    await waitFor(() => expect(
      window.localStorage.getItem("nanobot.skillEvolution.activeTask"),
    ).toBe("task-1"));
  });

  it("falls back to the server task list without deleting a temporarily unavailable pointer", async () => {
    const running = {
      ...analysisTask(),
      task_id: "task-recovered",
      status: "analyzing",
      active_analysis_id: null,
      analyses: [],
    } satisfies SkillEvolutionTask;
    window.localStorage.setItem("nanobot.skillEvolution.activeTask", "task-stored");
    vi.mocked(fetchSkillEvolutionTask)
      .mockRejectedValueOnce(new Error("temporary request failure"))
      .mockResolvedValue(running);
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([running]);

    renderPage();

    expect(await screen.findByText("analyzing")).toBeInTheDocument();
    expect(fetchSkillEvolutionTasks).toHaveBeenCalledWith("token");
    await waitFor(() => expect(
      window.localStorage.getItem("nanobot.skillEvolution.activeTask"),
    ).toBe("task-recovered"));
  });

  it("prefers a newer server task over a stale local pointer", async () => {
    const stale = {
      ...analysisTask(),
      task_id: "task-stale",
      created_at: "2025-01-01T00:00:00Z",
    } satisfies SkillEvolutionTask;
    const running = {
      ...analysisTask(),
      task_id: "task-newest",
      status: "collecting_evidence",
      active_analysis_id: null,
      analyses: [],
      created_at: "2026-01-02T00:00:00Z",
    } satisfies SkillEvolutionTask;
    window.localStorage.setItem("nanobot.skillEvolution.activeTask", "task-stale");
    vi.mocked(fetchSkillEvolutionTask).mockImplementation(async (_token, taskId) =>
      taskId === "task-stale" ? stale : running
    );
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([running, stale]);

    renderPage();

    expect(await screen.findByText("collecting_evidence")).toBeInTheDocument();
    await waitFor(() => expect(
      window.localStorage.getItem("nanobot.skillEvolution.activeTask"),
    ).toBe("task-newest"));
  });

  it("analyzes Cases, shows categories, and disables non-Skill owners", async () => {
    renderPage();
    fireEvent.click(await screen.findByLabelText("Case 1"));
    fireEvent.click(screen.getByRole("button", { name: /Analyze 1 low-scoring Cases/ }));

    expect(await screen.findByText("Grounded completeness workflow")).toBeInTheDocument();
    expect(screen.getByLabelText("Category c1")).toBeChecked();
    expect(screen.getByLabelText("Category c2")).toBeDisabled();
    expect(analyzeSkillEvolution).toHaveBeenCalledWith(
      "token", "job-1", 0.6, "gpt-5-6-luna", "gpt-5-6-sol", ["1"],
    );
  });

  it("starts one restricted edit from selected reason categories", async () => {
    renderPage();
    fireEvent.click(await screen.findByLabelText("Case 1"));
    fireEvent.click(screen.getByRole("button", { name: /Analyze 1 low-scoring Cases/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Modify Skill from 1 reason categories/ }));

    await waitFor(() => expect(evolveSkillEvolution).toHaveBeenCalledWith(
      "token", "task-1", "a1", "analysis-digest", ["c1"], "categories",
    ));
  });

  it("shows reanalysis immediately and hides stale candidate controls", async () => {
    const previous = {
      ...analysisTask(),
      status: "test_failed",
      active_revision_id: "r1",
      revisions: [{
        revision_id: "r1",
        status: "test_failed",
        summary: "Previous candidate",
        changed_paths: ["SKILL.md"],
        candidate_digest: "candidate-digest",
        diff: "+ previous candidate",
        validation: { valid: true, errors: [] },
        test_results: [],
      }],
    } satisfies SkillEvolutionTask;
    const running = {
      ...previous,
      status: "collecting_evidence",
      phase: "collecting_evidence",
      error: null,
    } satisfies SkillEvolutionTask;
    let resolveAction: (value: SkillEvolutionTask) => void = () => undefined;
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([previous]);
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(previous);
    vi.mocked(fetchSkillEvolutionActivities).mockResolvedValue({ cursor: 0, activities: [] });
    vi.mocked(runSkillEvolutionAction).mockReturnValue(new Promise((resolve) => {
      resolveAction = resolve;
    }));

    renderPage();

    expect(await screen.findByRole("button", { name: "Continue editing this candidate" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reanalyze Bad Cases" }));

    expect(screen.getByRole("status")).toHaveTextContent("Starting a fresh Bad Case analysis");
    expect(screen.queryByRole("button", { name: "Continue editing this candidate" })).not.toBeInTheDocument();
    expect(runSkillEvolutionAction).toHaveBeenCalledWith(
      "token", "task-1", "reanalyze", "r1", [], "categories",
    );

    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(running);
    resolveAction(running);
    expect(await screen.findByText("Reanalyzing Bad Cases")).toBeInTheDocument();
    expect(screen.queryByText("Previous candidate")).not.toBeInTheDocument();
  });

  it("blocks editing and resumes categorization when categories are incomplete", async () => {
    const base = analysisTask();
    const incomplete = {
      ...base,
      status: "failed",
      error: "Request timed out.",
      analyses: [{
        ...base.analyses[0],
        categories: [],
      }],
    } satisfies SkillEvolutionTask;
    const running = {
      ...incomplete,
      status: "analyzing",
      error: null,
    } satisfies SkillEvolutionTask;
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([incomplete]);
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(incomplete);
    vi.mocked(runSkillEvolutionAction).mockResolvedValue(running);

    renderPage();

    const continueButton = await screen.findByRole("button", {
      name: "Continue reason categorization",
    });
    expect(screen.getByText(/Cross-batch reason categorization is incomplete/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Modify Skill/ })).toBeDisabled();

    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(running);
    vi.mocked(fetchSkillEvolutionActivities).mockResolvedValue({ cursor: 0, activities: [] });
    fireEvent.click(continueButton);

    await waitFor(() => expect(runSkillEvolutionAction).toHaveBeenCalledWith(
      "token", "task-1", "reanalyze", "r1", [], "findings",
    ));
    expect(await screen.findByText("Generating reason categories")).toBeInTheDocument();
  });

  it("shows probe status and per-category retest deltas", async () => {
    const base = analysisTask();
    const tested = {
      ...base,
      status: "tested",
      active_revision_id: "r1",
      revisions: [{
        revision_id: "r1",
        status: "tested",
        summary: "Candidate updated",
        changed_paths: ["SKILL.md", "scripts/inspect.py"],
        candidate_digest: "candidate-digest",
        diff: "+ deterministic inspection",
        validation: { valid: true, errors: [] },
        intervention_validation: {
          valid: true,
          errors: [],
          probe_results: [{
            category_id: "c1",
            case_id: "1",
            assets: ["input.xlsx"],
            returncode: 0,
            valid: true,
            missing_fields: [],
          }],
        },
        category_ids: ["c1"],
        target_case_ids: ["1"],
        test_results: [{
          case_key: ["ocb", "officecli", "gpt-5-6-luna", "1"].join("\0"),
          case_id: "1",
          benchmark: "ocb",
          model_preset: "gpt-5-6-luna",
          baseline_score: 0.5,
          evolved_score: 0.6,
          delta: 0.1,
          status: "completed",
          scope: "target",
          category_ids: ["c1"],
        }],
        recommendation: {
          recommended: true,
          all_target_cases_scored: true,
          mean_delta: 0.1,
          improved_cases: 1,
          unchanged_cases: 0,
          regressed_cases: 0,
          category_summaries: [{
            category_id: "c1",
            case_count: 1,
            mean_delta: 0.1,
            improved_cases: 1,
            unchanged_cases: 0,
            regressed_cases: 0,
          }],
          disclaimer: "Historical baseline, single run, no global regression validation.",
        },
      }],
    } satisfies SkillEvolutionTask;
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([tested]);
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(tested);

    renderPage();

    expect(await screen.findByText("Probes passed")).toBeInTheDocument();
    expect(screen.getByText((_content, element) =>
      element?.tagName === "P" && element.textContent?.includes("Mean change: 0.100") === true
    )).toBeInTheDocument();
    expect(screen.getByText("c1 +0.100")).toBeInTheDocument();
    expect(screen.getByText(/Historical baseline, single run/)).toBeInTheDocument();
  });

  it("allows retrying a revision after a previous intervention validation failure", async () => {
    const base = analysisTask();
    const failedValidation = {
      ...base,
      status: "ready_for_review",
      active_revision_id: "r1",
      revisions: [{
        revision_id: "r1",
        status: "ready_for_review",
        summary: "Candidate updated",
        changed_paths: ["scripts/inspect.py"],
        candidate_digest: "candidate-digest",
        diff: "+ deterministic inspection",
        validation: { valid: true, errors: [] },
        intervention_validation: {
          valid: false,
          errors: ["stale validation failure"],
          probe_results: [],
        },
        category_ids: ["c1"],
        target_case_ids: ["1"],
        test_results: [],
      }],
    } satisfies SkillEvolutionTask;
    vi.mocked(fetchSkillEvolutionTasks).mockResolvedValue([failedValidation]);
    vi.mocked(fetchSkillEvolutionTask).mockResolvedValue(failedValidation);
    vi.mocked(runSkillEvolutionAction).mockResolvedValue(failedValidation);

    renderPage();

    const testButton = await screen.findByRole("button", { name: "Test selected Cases" });
    expect(testButton).toBeEnabled();
    fireEvent.click(testButton);
    await waitFor(() => expect(runSkillEvolutionAction).toHaveBeenCalledWith(
      "token", "task-1", "test", "r1", [], "categories",
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

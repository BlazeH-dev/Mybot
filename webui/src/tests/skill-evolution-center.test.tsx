import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SkillEvolutionCenter } from "@/components/evaluations/SkillEvolutionCenter";
import {
  fetchEvaluationRuns,
  fetchSkillEvolutionBadCases,
  generateSkillEvolution,
  runSkillEvolutionAction,
} from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", () => ({
  fetchEvaluationRuns: vi.fn(),
  fetchSkillEvolutionBadCases: vi.fn(),
  fetchSkillEvolutionTask: vi.fn(),
  generateSkillEvolution: vi.fn(),
  runSkillEvolutionAction: vi.fn(),
}));

const client = {};

function renderPage() {
  render(
    <ClientProvider client={client as never} token="token">
      <SkillEvolutionCenter />
    </ClientProvider>,
  );
}

describe("SkillEvolutionCenter", () => {
  beforeEach(() => {
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
    vi.mocked(generateSkillEvolution).mockResolvedValue({
      task_id: "task-1",
      title: "OfficeCLI evaluation-driven evolution",
      source_run_id: "job-1",
      source_profile: "office-release",
      base_skill: "officecli",
      derived_skill: "officecli-evolved",
      optimizer_model: "gpt-5-6-sol",
      threshold: 0.6,
      status: "ready_for_review",
      selected_cases: [],
      active_revision_id: "r1",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      revisions: [{
        revision_id: "r1",
        status: "ready_for_review",
        summary: "Add a deterministic inspection workflow",
        changed_paths: ["SKILL.md"],
        candidate_digest: "digest",
        diff: "--- a/SKILL.md\n+++ b/SKILL.md",
        validation: { valid: true, errors: [] },
        test_results: [],
      }],
    });
    vi.mocked(runSkillEvolutionAction).mockImplementation(async (_token, _task, action) => ({
      ...(await vi.mocked(generateSkillEvolution).mock.results[0]?.value),
      status: action === "test" ? "testing" : "applied",
    } as never));
  });

  it("selects low-scoring Cases and shows the candidate diff", async () => {
    renderPage();

    expect(await screen.findByText("gpt-5-6-luna")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Case 1"));
    fireEvent.click(screen.getByRole("button", { name: /Improve 1 selected Cases/ }));

    expect(await screen.findByText("Add a deterministic inspection workflow")).toBeInTheDocument();
    expect(screen.getByText("SKILL.md")).toBeInTheDocument();
    expect(generateSkillEvolution).toHaveBeenCalledWith(
      "token",
      "job-1",
      0.6,
      [["ocb", "officecli", "gpt-5-6-luna", "1"].join("\0")],
    );
  });

  it("starts selected-Case testing from a reviewed revision", async () => {
    renderPage();
    fireEvent.click(await screen.findByLabelText("Case 1"));
    fireEvent.click(await screen.findByRole("button", { name: /Improve 1 selected Cases/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Test selected Cases" }));

    await waitFor(() => expect(runSkillEvolutionAction).toHaveBeenCalledWith(
      "token", "task-1", "test", "r1",
    ));
  });

  it("limits one evolution round to 20 selected Cases", async () => {
    vi.mocked(fetchSkillEvolutionBadCases).mockResolvedValue({
      threshold: 0.6,
      cases: Array.from({ length: 21 }, (_, index) => ({
        case_key: ["ocb", "officecli", "gpt-5-6-luna", String(index + 1)].join("\0"),
        case_id: String(index + 1),
        benchmark: "ocb",
        skill: "officecli",
        model_preset: "gpt-5-6-luna",
        status: "completed",
        score: 0.25,
      })),
    });
    renderPage();

    await screen.findByLabelText("Case 21");
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));

    expect(screen.getByText("20/21")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deselect all" })).toBeInTheDocument();
    expect(screen.getByLabelText("Case 21")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Deselect all" }));

    expect(screen.getByText("0/21")).toBeInTheDocument();
    expect(screen.getByLabelText("Case 21")).toBeEnabled();
  });
});

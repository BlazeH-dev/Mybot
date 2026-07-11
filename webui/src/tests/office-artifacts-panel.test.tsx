import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  extractOfficeArtifacts,
  OfficeArtifactsPanel,
} from "@/components/OfficeArtifactsPanel";

describe("OfficeArtifactsPanel", () => {
  it("extracts and orders office artifacts by task", () => {
    const groups = extractOfficeArtifacts(`
      Done:
      - .nanobot-runtime/artifacts/task_weekly/weekly_review.pptx
      - .nanobot-runtime/artifacts/task_weekly/plan.json
      - .nanobot-runtime/artifacts/task_weekly/quality_report.json
      - .nanobot-runtime/artifacts/task_weekly/verified_facts.json
      - .nanobot-runtime/artifacts/task_weekly/weekly_review.pptx.officecli-batch.json
      - .nanobot-runtime/artifacts/task_weekly/weekly_review.pptx.officecli-validation.json
      - .nanobot-runtime/artifacts/task_weekly/weekly_review.pptx.officecli-run.json
      - .nanobot-runtime/artifacts/task_weekly/previews/weekly_review.png
      - .nanobot-runtime/artifacts/task_weekly/weekly_report.docx
      - .nanobot-runtime/artifacts/task_weekly/weekly_report.docx
    `);

    expect(groups).toHaveLength(1);
    expect(groups[0].taskId).toBe("task_weekly");
    expect(groups[0].artifacts.map((artifact) => artifact.fileName)).toEqual([
      "plan.json",
      "quality_report.json",
      "weekly_report.docx",
      "weekly_review.pptx",
      "verified_facts.json",
      "weekly_review.pptx.officecli-batch.json",
      "weekly_review.pptx.officecli-validation.json",
      "weekly_review.pptx.officecli-run.json",
      "weekly_review.png",
    ]);
  });

  it("labels OfficeCLI reproducibility artifacts", () => {
    render(
      <OfficeArtifactsPanel
        content={[
          ".nanobot-runtime/artifacts/task_weekly/weekly_report.docx.officecli-batch.json",
          ".nanobot-runtime/artifacts/task_weekly/weekly_report.docx.officecli-validation.json",
          ".nanobot-runtime/artifacts/task_weekly/weekly_report.docx.officecli-run.json",
          ".nanobot-runtime/artifacts/task_weekly/previews/weekly_report.png",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("OfficeCLI batch")).toBeInTheDocument();
    expect(screen.getByText("OpenXML validation")).toBeInTheDocument();
    expect(screen.getByText("OfficeCLI run")).toBeInTheDocument();
    expect(screen.getByText("Rendered preview")).toBeInTheDocument();
  });

  it("renders nothing for non-office replies", () => {
    const { container } = render(
      <OfficeArtifactsPanel content="No artifact paths here." />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders artifact buttons and opens previews", () => {
    const onOpenFilePreview = vi.fn();
    const docxPath = "/Users/me/project/.nanobot-runtime/artifacts/task_weekly/weekly_report.docx";
    const pptxPath = "/Users/me/project/.nanobot-runtime/artifacts/task_weekly/weekly_review.pptx";

    render(
      <OfficeArtifactsPanel
        content={`Artifacts: ${docxPath}\n${pptxPath}`}
        onOpenFilePreview={onOpenFilePreview}
      />,
    );

    expect(screen.getByTestId("office-artifacts-panel")).toBeInTheDocument();
    expect(screen.getByText("Office outputs")).toBeInTheDocument();
    expect(screen.getByText("Word report")).toBeInTheDocument();
    expect(screen.getByText("PowerPoint deck")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open weekly_report.docx" }));

    expect(onOpenFilePreview).toHaveBeenCalledWith(docxPath);
  });
});

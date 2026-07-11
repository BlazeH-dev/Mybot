import { useMemo } from "react";
import {
  Activity,
  BarChart3,
  ClipboardCheck,
  ClipboardList,
  Eye,
  FileJson,
  FileText,
  FolderKanban,
  Presentation,
  TableProperties,
  Workflow,
} from "lucide-react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export type OfficeArtifactKind =
  | "plan"
  | "quality"
  | "facts"
  | "report"
  | "deck"
  | "batch"
  | "engineValidation"
  | "engineRun"
  | "preview"
  | "dsl"
  | "schema"
  | "workbook"
  | "other";

export interface OfficeArtifact {
  path: string;
  taskId: string;
  fileName: string;
  kind: OfficeArtifactKind;
  label: string;
}

export interface OfficeArtifactGroup {
  taskId: string;
  artifacts: OfficeArtifact[];
}

interface OfficeArtifactsPanelProps {
  content: string;
  onOpenFilePreview?: (path: string) => void;
}

const OFFICE_PATH_RE =
  /((?:[A-Za-z]:)?(?:[^\s`"'<>)[\]]*[\\/])?\.nanobot-runtime[\\/]+artifacts[\\/]+[^\s`"'<>)[\]]+[\\/]+[^\s`"'<>)[\]]+)/g;

const KIND_ORDER: Record<OfficeArtifactKind, number> = {
  plan: 0,
  quality: 1,
  report: 2,
  deck: 3,
  facts: 4,
  batch: 5,
  engineValidation: 6,
  engineRun: 7,
  preview: 8,
  dsl: 9,
  schema: 10,
  workbook: 11,
  other: 12,
};

export function extractOfficeArtifacts(content: string): OfficeArtifactGroup[] {
  const byTask = new Map<string, OfficeArtifact[]>();
  const seen = new Set<string>();

  for (const match of content.matchAll(OFFICE_PATH_RE)) {
    const path = cleanArtifactPath(match[1]);
    const artifact = artifactFromPath(path);
    if (!artifact || seen.has(artifact.path)) continue;
    seen.add(artifact.path);
    const artifacts = byTask.get(artifact.taskId) ?? [];
    artifacts.push(artifact);
    byTask.set(artifact.taskId, artifacts);
  }

  return Array.from(byTask.entries()).map(([taskId, artifacts]) => ({
    taskId,
    artifacts: artifacts.sort((a, b) => {
      const byKind = KIND_ORDER[a.kind] - KIND_ORDER[b.kind];
      return byKind || a.fileName.localeCompare(b.fileName);
    }),
  }));
}

export function OfficeArtifactsPanel({
  content,
  onOpenFilePreview,
}: OfficeArtifactsPanelProps) {
  const groups = useMemo(() => extractOfficeArtifacts(content), [content]);
  if (groups.length === 0) return null;
  const total = groups.reduce((count, group) => count + group.artifacts.length, 0);

  return (
    <div
      data-testid="office-artifacts-panel"
      className={cn(
        "not-prose mt-3 overflow-hidden rounded-lg border border-border/65",
        "bg-muted/20 text-sm shadow-[0_8px_24px_-22px_rgba(0,0,0,0.45)]",
      )}
    >
      <div className="flex min-h-11 items-center gap-2 border-b border-border/55 px-3">
        <FolderKanban className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-300" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium text-foreground">
            Office outputs
          </div>
          <div className="truncate text-[11px] text-muted-foreground">
            {total} {total === 1 ? "artifact" : "artifacts"}
          </div>
        </div>
      </div>
      <div className="divide-y divide-border/45">
        {groups.map((group) => (
          <div key={group.taskId} className="px-3 py-2.5">
            <div className="mb-2 flex min-w-0 items-center gap-2 text-[11px] text-muted-foreground">
              <span className="shrink-0 font-medium text-foreground/75">task</span>
              <code className="min-w-0 truncate rounded bg-background/70 px-1.5 py-0.5 font-mono">
                {group.taskId}
              </code>
            </div>
            <div className="grid gap-1.5 sm:grid-cols-2">
              {group.artifacts.map((artifact) => (
                <ArtifactButton
                  key={artifact.path}
                  artifact={artifact}
                  onOpenFilePreview={onOpenFilePreview}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ArtifactButton({
  artifact,
  onOpenFilePreview,
}: {
  artifact: OfficeArtifact;
  onOpenFilePreview?: (path: string) => void;
}) {
  const Icon = iconForKind(artifact.kind);
  const interactive = Boolean(onOpenFilePreview);
  const body = (
    <span
      className={cn(
        "flex h-11 min-w-0 items-center gap-2 rounded-md border border-border/55",
        "bg-background/72 px-2.5 text-left transition-colors",
        interactive && "hover:border-sky-300/70 hover:bg-sky-50/75 dark:hover:border-sky-300/30 dark:hover:bg-sky-300/10",
      )}
    >
      <span
        className={cn(
          "grid h-7 w-7 shrink-0 place-items-center rounded-md",
          iconToneForKind(artifact.kind),
        )}
        aria-hidden
      >
        <Icon className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[12px] font-medium text-foreground">
          {artifact.label}
        </span>
        <span className="block truncate font-mono text-[11px] text-muted-foreground">
          {artifact.fileName}
        </span>
      </span>
    </span>
  );

  return (
    <TooltipProvider delayDuration={400} skipDelayDuration={80}>
      <Tooltip>
        <TooltipTrigger asChild>
          {interactive ? (
            <button
              type="button"
              aria-label={`Open ${artifact.fileName}`}
              className="min-w-0 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => onOpenFilePreview?.(artifact.path)}
            >
              {body}
            </button>
          ) : (
            <div className="min-w-0">{body}</div>
          )}
        </TooltipTrigger>
        <TooltipContent
          side="top"
          align="center"
          sideOffset={8}
          className="max-w-[min(38rem,calc(100vw-2rem))] break-all font-mono text-[11px]"
        >
          {artifact.path}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function cleanArtifactPath(value: string): string {
  return value
    .trim()
    .replace(/[.,;!?]+$/, "")
    .replace(/:\d+(?::\d+)?$/, "")
    .replace(/\\/g, "/");
}

function artifactFromPath(path: string): OfficeArtifact | null {
  const match = /(?:^|\/)\.nanobot-runtime\/artifacts\/([^/]+)\/(.+)$/.exec(path);
  if (!match) return null;
  const taskId = match[1];
  const fileName = match[2].split("/").pop() ?? "";
  if (!fileName || !/\.(json|docx|pptx|xlsx|md|png)$/i.test(fileName)) return null;
  const kind = classifyArtifact(fileName);
  return {
    path,
    taskId,
    fileName,
    kind,
    label: labelForKind(kind, fileName),
  };
}

function classifyArtifact(fileName: string): OfficeArtifactKind {
  const lower = fileName.toLowerCase();
  if (lower === "plan.json") return "plan";
  if (lower === "quality_report.json") return "quality";
  if (lower === "verified_facts.json") return "facts";
  if (lower === "workbook_schema.json") return "schema";
  if (lower.endsWith(".officecli-batch.json")) return "batch";
  if (lower.endsWith(".officecli-validation.json")) return "engineValidation";
  if (lower.endsWith(".officecli-run.json")) return "engineRun";
  if (lower.endsWith(".png")) return "preview";
  if (lower.endsWith("_dsl.json")) return "dsl";
  if (lower.endsWith(".docx")) return "report";
  if (lower.endsWith(".pptx")) return "deck";
  if (lower.endsWith(".xlsx")) return "workbook";
  return "other";
}

function labelForKind(kind: OfficeArtifactKind, fileName: string): string {
  switch (kind) {
    case "plan":
      return "Plan";
    case "quality":
      return "Quality report";
    case "facts":
      return "Verified facts";
    case "report":
      return "Word report";
    case "deck":
      return "PowerPoint deck";
    case "batch":
      return "OfficeCLI batch";
    case "engineValidation":
      return "OpenXML validation";
    case "engineRun":
      return "OfficeCLI run";
    case "preview":
      return "Rendered preview";
    case "dsl":
      return fileName.includes("slide") ? "Slide DSL" : "Report DSL";
    case "schema":
      return "Workbook schema";
    case "workbook":
      return "Workbook";
    default:
      return "Artifact";
  }
}

function iconForKind(kind: OfficeArtifactKind) {
  switch (kind) {
    case "plan":
      return ClipboardList;
    case "quality":
      return ClipboardCheck;
    case "facts":
      return BarChart3;
    case "report":
      return FileText;
    case "deck":
      return Presentation;
    case "batch":
      return Workflow;
    case "engineValidation":
      return ClipboardCheck;
    case "engineRun":
      return Activity;
    case "preview":
      return Eye;
    case "schema":
    case "workbook":
      return TableProperties;
    default:
      return FileJson;
  }
}

function iconToneForKind(kind: OfficeArtifactKind): string {
  switch (kind) {
    case "quality":
      return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
    case "report":
      return "bg-blue-500/10 text-blue-700 dark:text-blue-300";
    case "deck":
      return "bg-rose-500/10 text-rose-700 dark:text-rose-300";
    case "facts":
      return "bg-amber-500/12 text-amber-700 dark:text-amber-300";
    case "plan":
      return "bg-violet-500/10 text-violet-700 dark:text-violet-300";
    case "batch":
    case "engineRun":
      return "bg-cyan-500/10 text-cyan-700 dark:text-cyan-300";
    case "engineValidation":
      return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
    case "preview":
      return "bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300";
    default:
      return "bg-muted text-muted-foreground";
  }
}

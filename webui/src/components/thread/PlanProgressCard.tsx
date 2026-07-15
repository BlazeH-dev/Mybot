import { useEffect, useState } from "react";
import {
  Check,
  Circle,
  CircleDot,
  ListTodo,
  Play,
  SkipForward,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

export type PlanStepStatus = "pending" | "in_progress" | "done" | "skipped";

export interface PlanStepSnapshot {
  id: string;
  description: string;
  status: PlanStepStatus;
  expectedArtifacts: string[];
}

export interface PlanSnapshot {
  taskId: string;
  goal: string;
  status: "creating" | "awaiting_confirmation" | "active" | "completed";
  planHash: string;
  path?: string;
  steps: PlanStepSnapshot[];
}

function record(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function jsonRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "string") return record(value);
  try {
    return record(JSON.parse(value));
  } catch {
    return null;
  }
}

function eventName(event: ToolProgressEvent): string {
  const fnName = (event as { function?: { name?: unknown } }).function?.name;
  if (typeof fnName === "string") return fnName;
  return typeof event.name === "string" ? event.name : "";
}

function eventArguments(event: ToolProgressEvent): Record<string, unknown> | null {
  const fnArgs = (event as { function?: { arguments?: unknown } }).function?.arguments;
  return jsonRecord(fnArgs ?? event.arguments);
}

function normalizeStep(value: unknown): PlanStepSnapshot | null {
  const step = record(value);
  if (!step) return null;
  const id = typeof step.id === "string" ? step.id.trim() : "";
  const description = typeof step.description === "string" ? step.description.trim() : "";
  if (!id || !description) return null;
  const rawStatus = typeof step.status === "string" ? step.status : "pending";
  const status: PlanStepStatus = ["pending", "in_progress", "done", "skipped"].includes(rawStatus)
    ? rawStatus as PlanStepStatus
    : "pending";
  const expectedArtifacts = Array.isArray(step.expected_artifacts)
    ? step.expected_artifacts.filter((item): item is string => typeof item === "string")
    : [];
  return { id, description, status, expectedArtifacts };
}

function snapshotFromPlan(value: unknown, path?: string): PlanSnapshot | null {
  const plan = record(value);
  if (!plan) return null;
  const taskId = typeof plan.task_id === "string" ? plan.task_id : "";
  const goal = typeof plan.goal === "string" ? plan.goal : "";
  const planHash = typeof plan.plan_hash === "string" ? plan.plan_hash : "";
  const rawStatus = typeof plan.status === "string" ? plan.status : "creating";
  const status = ["awaiting_confirmation", "active", "completed"].includes(rawStatus)
    ? rawStatus as PlanSnapshot["status"]
    : "creating";
  const steps = Array.isArray(plan.steps)
    ? plan.steps.map(normalizeStep).filter((step): step is PlanStepSnapshot => step != null)
    : [];
  if (!taskId && !goal && steps.length === 0) return null;
  return { taskId, goal, status, planHash, path, steps };
}

function snapshotFromEvent(event: ToolProgressEvent): PlanSnapshot | null {
  if (eventName(event) !== "plan") return null;
  const result = jsonRecord(event.result);
  if (result) {
    const path = typeof result.path === "string" ? result.path : undefined;
    const snapshot = snapshotFromPlan(result.plan ?? result, path);
    if (snapshot) return snapshot;
  }
  const args = eventArguments(event);
  if (!args || args.action !== "create") return null;
  return snapshotFromPlan({
    task_id: args.task_id,
    goal: args.goal,
    steps: args.steps,
    status: "creating",
    plan_hash: "",
  });
}

export function latestPlanSnapshot(messages: UIMessage[]): PlanSnapshot | null {
  let latest: PlanSnapshot | null = null;
  for (const message of messages) {
    for (const event of message.toolEvents ?? []) {
      latest = snapshotFromEvent(event) ?? latest;
    }
  }
  return latest;
}

function statusIcon(status: PlanStepStatus) {
  if (status === "done") return <Check className="h-3.5 w-3.5" aria-hidden />;
  if (status === "in_progress") return <CircleDot className="h-3.5 w-3.5" aria-hidden />;
  if (status === "skipped") return <SkipForward className="h-3.5 w-3.5" aria-hidden />;
  return <Circle className="h-3.5 w-3.5" aria-hidden />;
}

export function PlanProgressCard({
  plan,
  onExecute,
}: {
  plan: PlanSnapshot;
  onExecute?: (taskId: string, planHash: string) => void;
}) {
  const { t } = useTranslation();
  const [executePending, setExecutePending] = useState(false);

  useEffect(() => setExecutePending(false), [plan.planHash, plan.status]);

  const statusLabel = t(`message.plan.status.${plan.status}`);
  const canExecute =
    plan.status === "awaiting_confirmation" && !!plan.taskId && !!plan.planHash && !!onExecute;

  return (
    <section
      className="my-3 overflow-hidden rounded-2xl border border-border/70 bg-card/85 shadow-[0_8px_24px_rgba(15,23,42,0.06)]"
      aria-label={t("message.plan.title")}
      data-testid="plan-progress-card"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border/55 px-4 py-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-blue-500/10 text-blue-600 dark:text-blue-300">
            <ListTodo className="h-4 w-4" aria-hidden />
          </span>
          <div className="min-w-0">
            <div className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              {t("message.plan.title")}
            </div>
            <div className="mt-0.5 text-sm font-medium text-foreground">
              {plan.goal || plan.taskId}
            </div>
          </div>
        </div>
        <span className="shrink-0 rounded-full bg-muted px-2.5 py-1 text-[11px] font-medium text-muted-foreground">
          {statusLabel}
        </span>
      </div>
      <ol className="space-y-1 px-4 py-3">
        {plan.steps.map((step) => (
          <li key={step.id} className="flex items-start gap-2.5 rounded-lg px-1 py-1.5">
            <span
              className={cn(
                "mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border",
                step.status === "done"
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600"
                  : step.status === "in_progress"
                    ? "border-blue-500/45 bg-blue-500/10 text-blue-600"
                    : "border-border text-muted-foreground",
              )}
            >
              {statusIcon(step.status)}
            </span>
            <div className="min-w-0 flex-1">
              <div
                className={cn(
                  "text-sm leading-5",
                  step.status === "done" && "text-muted-foreground line-through decoration-border",
                )}
              >
                {step.description}
              </div>
              {step.expectedArtifacts.length > 0 ? (
                <div className="mt-0.5 truncate text-[11px] text-muted-foreground/80">
                  {step.expectedArtifacts.join(" · ")}
                </div>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      {canExecute ? (
        <div className="flex justify-end border-t border-border/55 px-4 py-3">
          <Button
            type="button"
            size="sm"
            disabled={executePending}
            onClick={() => {
              setExecutePending(true);
              onExecute?.(plan.taskId, plan.planHash);
            }}
            className="rounded-full"
          >
            <Play className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {executePending ? t("message.plan.starting") : t("message.plan.execute")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

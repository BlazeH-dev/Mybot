import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  ChevronUp,
  Circle,
  CircleDot,
  Clock3,
  FileText,
  ListTodo,
  LoaderCircle,
  Play,
  SkipForward,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { SubagentActivity, ToolProgressEvent, UIMessage } from "@/lib/types";

export type PlanStepStatus =
  | "pending"
  | "ready"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked"
  | "uncertain"
  | "cancelled";

export interface PlanStepSnapshot {
  id: string;
  description: string;
  status: PlanStepStatus;
  expectedArtifacts: string[];
  dependsOn: string[];
  executor: "parent" | "child";
  childId?: string;
  error?: string;
}

export interface PlanSnapshot {
  taskId: string;
  goal: string;
  status:
    | "creating"
    | "awaiting_confirmation"
    | "awaiting_revision_confirmation"
    | "active"
    | "completed";
  planHash: string;
  revision: number;
  updatedAt?: string;
  path?: string;
  markdownPath?: string;
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
  const legacyStatus: Record<string, PlanStepStatus> = {
    in_progress: "running",
    done: "succeeded",
    skipped: "cancelled",
  };
  const rawStatus = typeof step.status === "string" ? step.status : "pending";
  const normalizedStatus = legacyStatus[rawStatus] ?? rawStatus;
  const status: PlanStepStatus = [
    "pending", "ready", "running", "succeeded", "failed", "blocked", "uncertain", "cancelled",
  ].includes(normalizedStatus)
    ? normalizedStatus as PlanStepStatus
    : "pending";
  const expectedArtifacts = Array.isArray(step.expected_artifacts)
    ? step.expected_artifacts.filter((item): item is string => typeof item === "string")
    : [];
  const dependsOn = Array.isArray(step.depends_on)
    ? step.depends_on.filter((item): item is string => typeof item === "string")
    : [];
  const executor = step.executor === "child" ? "child" : "parent";
  const childId = typeof step.child_id === "string" ? step.child_id : undefined;
  const error = typeof step.error === "string" ? step.error : undefined;
  return { id, description, status, expectedArtifacts, dependsOn, executor, childId, error };
}

function snapshotFromPlan(value: unknown, path?: string): PlanSnapshot | null {
  const plan = record(value);
  if (!plan) return null;
  const taskId = typeof plan.task_id === "string" ? plan.task_id : "";
  const goal = typeof plan.goal === "string" ? plan.goal : "";
  const planHash = typeof plan.plan_hash === "string" ? plan.plan_hash : "";
  const revision = typeof plan.revision === "number" ? plan.revision : 1;
  const updatedAt = typeof plan.updated_at === "string" ? plan.updated_at : undefined;
  const rawStatus = typeof plan.status === "string" ? plan.status : "creating";
  const status = [
    "awaiting_confirmation",
    "awaiting_revision_confirmation",
    "active",
    "completed",
  ].includes(rawStatus)
    ? rawStatus as PlanSnapshot["status"]
    : "creating";
  const steps = Array.isArray(plan.steps)
    ? plan.steps.map(normalizeStep).filter((step): step is PlanStepSnapshot => step != null)
    : [];
  if (!taskId && !goal && steps.length === 0) return null;
  const markdown = record(plan.plan_markdown);
  const markdownPath = typeof markdown?.path === "string" ? markdown.path : undefined;
  return {
    taskId,
    goal,
    status,
    planHash,
    revision,
    updatedAt,
    path,
    markdownPath,
    steps,
  };
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
  if (status === "succeeded") return <Check className="h-3.5 w-3.5" aria-hidden />;
  if (status === "running") return <CircleDot className="h-3.5 w-3.5" aria-hidden />;
  if (status === "cancelled") return <SkipForward className="h-3.5 w-3.5" aria-hidden />;
  if (["failed", "blocked", "uncertain"].includes(status)) {
    return <AlertTriangle className="h-3.5 w-3.5" aria-hidden />;
  }
  return <Circle className="h-3.5 w-3.5" aria-hidden />;
}

function topologicalBatches(steps: PlanStepSnapshot[]): PlanStepSnapshot[][] {
  const order = steps.map((step) => step.id);
  const remaining = new Map(steps.map((step) => [step.id, new Set(step.dependsOn)]));
  const resolved = new Set<string>();
  const batches: PlanStepSnapshot[][] = [];
  while (remaining.size > 0) {
    const ids = order.filter((id) => remaining.has(id)
      && Array.from(remaining.get(id) ?? []).every((dependency) => resolved.has(dependency)));
    if (ids.length === 0) return [steps];
    batches.push(ids.map((id) => steps.find((step) => step.id === id)!).filter(Boolean));
    ids.forEach((id) => {
      remaining.delete(id);
      resolved.add(id);
    });
  }
  return batches;
}

export function PlanProgressCard({
  plan,
  subagentActivities = [],
  onExecute,
  onOpenPlan,
  collapsed = false,
  onCollapsedChange,
}: {
  plan: PlanSnapshot;
  subagentActivities?: SubagentActivity[];
  onExecute?: (taskId: string, planHash: string, action: "confirm" | "resume") => void;
  onOpenPlan?: (path: string) => void;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
}) {
  const { t } = useTranslation();
  const [executePending, setExecutePending] = useState(false);
  const [subagentOpen, setSubagentOpen] = useState(false);
  const [selectedChildId, setSelectedChildId] = useState<string | null>(null);

  useEffect(
    () => setExecutePending(false),
    [plan.planHash, plan.status, plan.updatedAt],
  );

  const statusLabel = t(`message.plan.status.${plan.status}`, { defaultValue: plan.status });
  const canConfirm =
    ["awaiting_confirmation", "awaiting_revision_confirmation"].includes(plan.status)
    && !!plan.taskId && !!plan.planHash && !!onExecute;
  const canResume = plan.status === "active" && !!plan.taskId && !!plan.planHash && !!onExecute;
  const canExecute = canConfirm || canResume;
  const batches = topologicalBatches(plan.steps);
  const childSteps = plan.steps.filter((step) => step.executor === "child");
  const planActivities = useMemo(
    () => subagentActivities.filter((activity) => (
      activity.parentTaskId === plan.taskId
      && activity.planHash === plan.planHash
    )),
    [plan.planHash, plan.taskId, subagentActivities],
  );
  const runningChildren = planActivities.filter((activity) => activity.status === "running").length;
  const selectedActivity = planActivities.find((activity) => activity.childId === selectedChildId)
    ?? planActivities[0]
    ?? null;

  useEffect(() => {
    if (!planActivities.length) {
      setSelectedChildId(null);
      return;
    }
    if (!planActivities.some((activity) => activity.childId === selectedChildId)) {
      setSelectedChildId(planActivities[0].childId);
    }
  }, [planActivities, selectedChildId]);

  useEffect(() => {
    if (collapsed) setSubagentOpen(false);
  }, [collapsed]);

  if (collapsed) {
    return (
      <section
        className="flex h-11 items-center gap-3 overflow-hidden rounded-lg border border-border/70 bg-card px-3 shadow-sm"
        aria-label={t("message.plan.title")}
        data-testid="plan-progress-card"
        data-collapsed="true"
      >
        <ListTodo className="h-4 w-4 shrink-0 text-blue-600 dark:text-blue-300" aria-hidden />
        <div className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {plan.goal || plan.taskId}
        </div>
        <div className="shrink-0 font-mono text-[11px] text-muted-foreground">
          r{plan.revision} · {plan.planHash.slice(0, 12)}
        </div>
        <span className="shrink-0 rounded bg-muted px-2 py-1 text-[11px] font-medium text-muted-foreground">
          {statusLabel}
        </span>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="h-7 w-7 shrink-0"
          onClick={() => onCollapsedChange?.(false)}
          aria-label={t("message.plan.show", { defaultValue: "Show plan" })}
          title={t("message.plan.show", { defaultValue: "Show plan" })}
        >
          <ChevronDown className="h-4 w-4" aria-hidden />
        </Button>
      </section>
    );
  }

  return (
    <section
      className="overflow-hidden rounded-lg border border-border/70 bg-card shadow-sm"
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
            <div className="mt-1 font-mono text-[11px] text-muted-foreground">
              r{plan.revision} · {plan.planHash.slice(0, 12)}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="rounded bg-muted px-2.5 py-1 text-[11px] font-medium text-muted-foreground">
            {statusLabel}
          </span>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            onClick={() => onCollapsedChange?.(true)}
            aria-label={t("message.plan.hide", { defaultValue: "Hide plan" })}
            title={t("message.plan.hide", { defaultValue: "Hide plan" })}
          >
            <ChevronUp className="h-4 w-4" aria-hidden />
          </Button>
        </div>
      </div>
      <div className="space-y-3 px-4 py-3">
        {batches.map((batch, batchIndex) => (
          <div key={`batch-${batchIndex}`}>
            <div className="mb-1.5 text-[11px] font-medium text-muted-foreground">
              {t("message.plan.batch", { index: batchIndex + 1, defaultValue: `Batch ${batchIndex + 1}` })}
            </div>
            <ol className="space-y-1">
              {batch.map((step) => (
          <li key={step.id} className="flex items-start gap-2.5 rounded-lg px-1 py-1.5">
            <span
              className={cn(
                "mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border",
                step.status === "succeeded"
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600"
                  : step.status === "running"
                    ? "border-blue-500/45 bg-blue-500/10 text-blue-600"
                    : ["failed", "blocked", "uncertain"].includes(step.status)
                      ? "border-amber-500/45 bg-amber-500/10 text-amber-700"
                    : "border-border text-muted-foreground",
              )}
            >
              {statusIcon(step.status)}
            </span>
            <div className="min-w-0 flex-1">
              <div
                className={cn(
                  "text-sm leading-5",
                  step.status === "succeeded" && "text-muted-foreground line-through decoration-border",
                )}
              >
                {step.description}
              </div>
              {step.expectedArtifacts.length > 0 ? (
                <div className="mt-0.5 truncate text-[11px] text-muted-foreground/80">
                  {step.expectedArtifacts.join(" · ")}
                </div>
              ) : null}
              <div className="mt-1 flex flex-wrap gap-1.5 text-[10px] text-muted-foreground">
                <span className="rounded border border-border/60 px-1.5 py-0.5">{step.executor}</span>
                {step.dependsOn.length > 0 ? (
                  <span className="rounded border border-border/60 px-1.5 py-0.5">
                    {t("message.plan.dependsOn", { defaultValue: "Depends on" })}: {step.dependsOn.join(", ")}
                  </span>
                ) : null}
              </div>
              {step.error ? <div className="mt-1 text-xs text-amber-700 dark:text-amber-300">{step.error}</div> : null}
            </div>
          </li>
              ))}
            </ol>
          </div>
        ))}
      </div>
      {canExecute || childSteps.length > 0 || (plan.markdownPath && onOpenPlan) ? (
        <div className="flex justify-end gap-2 border-t border-border/55 px-4 py-3">
          {childSteps.length > 0 ? (
            <Button type="button" size="sm" variant="outline" onClick={() => setSubagentOpen(true)}>
              <Bot className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {runningChildren > 0
                ? t("message.plan.subagentsRunning", {
                  count: runningChildren,
                  defaultValue: `${runningChildren} subagents running`,
                })
                : t("message.plan.openSubagents", { defaultValue: "View subagents" })}
            </Button>
          ) : null}
          {plan.markdownPath && onOpenPlan ? (
            <Button type="button" size="sm" variant="outline" onClick={() => onOpenPlan(plan.markdownPath!)}>
              <FileText className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {t("message.plan.openDocument", { defaultValue: "View full plan" })}
            </Button>
          ) : null}
          {canExecute ? (
          <Button
            type="button"
            size="sm"
            disabled={executePending}
            onClick={() => {
              setExecutePending(true);
              onExecute?.(plan.taskId, plan.planHash, canResume ? "resume" : "confirm");
            }}
            className="rounded-md"
          >
            <Play className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {executePending
              ? t(canResume ? "message.plan.resuming" : "message.plan.starting")
              : t(canResume ? "message.plan.resume" : "message.plan.execute")}
          </Button>
          ) : null}
        </div>
      ) : null}
      <SubagentActivitySheet
        open={subagentOpen}
        onOpenChange={setSubagentOpen}
        activities={planActivities}
        childSteps={childSteps}
        selectedChildId={selectedActivity?.childId ?? null}
        onSelect={setSelectedChildId}
      />
    </section>
  );
}

function formatElapsed(milliseconds: number): string {
  if (milliseconds < 1000) return `${milliseconds} ms`;
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function compactJson(value: unknown): string {
  if (value == null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.length > 3000 ? `${text.slice(0, 3000)}\n...` : text;
}

function SubagentActivitySheet({
  open,
  onOpenChange,
  activities,
  childSteps,
  selectedChildId,
  onSelect,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activities: SubagentActivity[];
  childSteps: PlanStepSnapshot[];
  selectedChildId: string | null;
  onSelect: (childId: string) => void;
}) {
  const { t } = useTranslation();
  const selected = activities.find((activity) => activity.childId === selectedChildId) ?? null;
  const totalTokens = selected
    ? Object.values(selected.usage).reduce((sum, value) => sum + (Number(value) || 0), 0)
    : 0;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[52rem] max-w-[52rem] gap-0 p-0 sm:max-w-[52rem]"
        aria-describedby={undefined}
      >
        <div className="border-b px-6 pb-4 pt-5">
          <SheetTitle className="flex items-center gap-2 text-base font-medium">
            <Bot className="h-4 w-4" aria-hidden />
            {t("message.plan.subagentPanel.title", { defaultValue: "Subagent execution" })}
          </SheetTitle>
          <SheetDescription className="sr-only">
            {t("message.plan.subagentPanel.description", {
              defaultValue: "Live child-agent reasoning, tool activity, status, and results.",
            })}
          </SheetDescription>
        </div>
        <div className="grid min-h-0 flex-1 grid-cols-[15rem_minmax(0,1fr)]">
          <div className="overflow-y-auto border-r px-2 py-3">
            {activities.length > 0 ? activities.map((activity) => (
              <button
                key={activity.childId}
                type="button"
                onClick={() => onSelect(activity.childId)}
                className={cn(
                  "mb-1 w-full rounded-md px-3 py-2.5 text-left transition-colors",
                  selectedChildId === activity.childId
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{activity.label}</span>
                  {activity.status === "running" ? (
                    <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden />
                  ) : activity.status === "completed" ? (
                    <Check className="h-3.5 w-3.5 shrink-0 text-emerald-600" aria-hidden />
                  ) : (
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden />
                  )}
                </div>
                <div className="mt-1 truncate font-mono text-[10px]">{activity.nodeId || activity.childId}</div>
              </button>
            )) : (
              <div className="px-3 py-5 text-xs leading-5 text-muted-foreground">
                {t("message.plan.subagentPanel.waiting", {
                  count: childSteps.length,
                  defaultValue: "Child steps are waiting to be dispatched.",
                })}
              </div>
            )}
          </div>
          <div className="min-h-0 overflow-y-auto px-6 py-5">
            {selected ? (
              <div className="space-y-6">
                <div>
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <h3 className="text-base font-semibold text-foreground">{selected.label}</h3>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">
                        {selected.taskDescription}
                      </p>
                    </div>
                    <span className="shrink-0 rounded border border-border px-2 py-1 text-xs text-muted-foreground">
                      {selected.status}
                    </span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
                    <span>{selected.nodeId || selected.childId}</span>
                    <span>{t("message.plan.subagentPanel.iteration", { defaultValue: "Iteration" })} {selected.iteration + 1}</span>
                    <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{formatElapsed(selected.elapsedMs)}</span>
                    {totalTokens > 0 ? <span>{totalTokens.toLocaleString()} tokens</span> : null}
                  </div>
                </div>

                <section>
                  <h4 className="flex items-center gap-2 text-xs font-semibold uppercase text-muted-foreground">
                    <BrainCircuit className="h-3.5 w-3.5" aria-hidden />
                    {t("message.plan.subagentPanel.reasoning", { defaultValue: "Reasoning" })}
                  </h4>
                  <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground">
                    {selected.reasoning || t("message.plan.subagentPanel.noReasoning", {
                      defaultValue: "No reasoning has been emitted yet.",
                    })}
                    {selected.reasoningStreaming ? (
                      <span className="ml-1 inline-block h-4 w-1 animate-pulse bg-foreground/55 align-middle" />
                    ) : null}
                  </div>
                </section>

                <section>
                  <h4 className="flex items-center gap-2 text-xs font-semibold uppercase text-muted-foreground">
                    <Wrench className="h-3.5 w-3.5" aria-hidden />
                    {t("message.plan.subagentPanel.tools", { defaultValue: "Tool activity" })}
                  </h4>
                  {selected.toolEvents.length > 0 ? (
                    <ol className="mt-2 divide-y divide-border/60 border-y border-border/60">
                      {selected.toolEvents.map((event, index) => (
                        <li key={event.call_id || `${event.name}-${index}`} className="py-3">
                          <div className="flex items-center justify-between gap-3 text-sm">
                            <span className="font-medium text-foreground">{event.name || "tool"}</span>
                            <span className="text-xs text-muted-foreground">{event.phase || "running"}</span>
                          </div>
                          {event.arguments != null ? (
                            <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-5 text-muted-foreground">
                              {compactJson(event.arguments)}
                            </pre>
                          ) : null}
                          {event.error ? (
                            <div className="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">
                              {compactJson(event.error)}
                            </div>
                          ) : null}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <div className="mt-2 text-sm text-muted-foreground">
                      {t("message.plan.subagentPanel.noTools", { defaultValue: "No tools used yet." })}
                    </div>
                  )}
                </section>

                {selected.finalResult || selected.error ? (
                  <section>
                    <h4 className="text-xs font-semibold uppercase text-muted-foreground">
                      {t("message.plan.subagentPanel.result", { defaultValue: "Result" })}
                    </h4>
                    <div className={cn(
                      "mt-2 whitespace-pre-wrap text-sm leading-6",
                      selected.error ? "text-amber-700 dark:text-amber-300" : "text-foreground",
                    )}>
                      {selected.finalResult || selected.error}
                    </div>
                  </section>
                ) : null}
              </div>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                {t("message.plan.subagentPanel.waiting", {
                  count: childSteps.length,
                  defaultValue: "Child steps are waiting to be dispatched.",
                })}
              </div>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}

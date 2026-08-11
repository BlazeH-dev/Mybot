import { mergeToolProgressEvents, normalizeToolProgressEvents } from "@/lib/tool-traces";
import type {
  SubagentActivity,
  SubagentActivityPayload,
  SubagentActivityStatus,
} from "@/lib/types";

function terminalStatus(event: string | undefined): SubagentActivityStatus | null {
  if (event === "completed") return "completed";
  if (event === "failed") return "failed";
  if (event === "cancelled") return "cancelled";
  return null;
}

export function activityFromPayload(payload: SubagentActivityPayload): SubagentActivity | null {
  const childId = payload.child_id?.trim();
  if (!childId) return null;
  const eventStatus = terminalStatus(payload.event);
  return {
    childId,
    label: payload.label?.trim() || childId,
    taskDescription: payload.task_description || "",
    parentTaskId: payload.parent_task_id,
    planHash: payload.plan_hash,
    nodeId: payload.node_id,
    status: eventStatus ?? payload.status ?? "running",
    phase: payload.phase || "initializing",
    iteration: Number.isFinite(payload.iteration) ? Math.max(0, payload.iteration ?? 0) : 0,
    elapsedMs: Number.isFinite(payload.elapsed_ms) ? Math.max(0, payload.elapsed_ms ?? 0) : 0,
    reasoning: payload.reasoning || payload.reasoning_delta || "",
    reasoningStreaming: eventStatus
      ? false
      : payload.event === "reasoning_delta" || payload.reasoning_streaming === true,
    toolEvents: normalizeToolProgressEvents(payload.tool_events),
    usage: payload.usage ?? {},
    finalResult: payload.final_result,
    error: payload.error,
    updatedAt: payload.updated_at,
  };
}

export function mergeSubagentActivity(
  current: SubagentActivity | undefined,
  payload: SubagentActivityPayload,
): SubagentActivity | null {
  const incoming = activityFromPayload(payload);
  if (!incoming) return current ?? null;
  if (!current) return incoming;
  const eventStatus = terminalStatus(payload.event);
  const hasReasoningSnapshot = typeof payload.reasoning === "string";
  return {
    ...current,
    ...incoming,
    label: payload.label ? incoming.label : current.label,
    taskDescription: payload.task_description !== undefined
      ? incoming.taskDescription
      : current.taskDescription,
    parentTaskId: payload.parent_task_id !== undefined
      ? incoming.parentTaskId
      : current.parentTaskId,
    planHash: payload.plan_hash !== undefined ? incoming.planHash : current.planHash,
    nodeId: payload.node_id !== undefined ? incoming.nodeId : current.nodeId,
    status: eventStatus ?? payload.status ?? current.status,
    phase: payload.phase ?? current.phase,
    iteration: payload.iteration ?? current.iteration,
    elapsedMs: payload.elapsed_ms ?? current.elapsedMs,
    reasoning: hasReasoningSnapshot
      ? payload.reasoning ?? ""
      : current.reasoning + (payload.reasoning_delta ?? ""),
    reasoningStreaming: eventStatus
      ? false
      : payload.event === "reasoning_end"
        ? false
        : payload.event === "reasoning_delta"
          ? true
          : payload.reasoning_streaming ?? current.reasoningStreaming,
    toolEvents: payload.tool_events
      ? mergeToolProgressEvents(current.toolEvents, incoming.toolEvents)
      : current.toolEvents,
    usage: payload.usage ?? current.usage,
    finalResult: payload.final_result !== undefined ? incoming.finalResult : current.finalResult,
    error: payload.error !== undefined ? incoming.error : current.error,
    updatedAt: payload.updated_at ?? current.updatedAt,
  };
}

export function mergeSubagentActivityList(
  current: SubagentActivity[],
  payload: SubagentActivityPayload,
): SubagentActivity[] {
  const index = current.findIndex((item) => item.childId === payload.child_id);
  const merged = mergeSubagentActivity(index >= 0 ? current[index] : undefined, payload);
  if (!merged) return current;
  if (index < 0) return [...current, merged];
  const next = [...current];
  next[index] = merged;
  return next;
}

export function activitiesFromPayloads(payloads: SubagentActivityPayload[] | undefined): SubagentActivity[] {
  return (payloads ?? []).reduce(mergeSubagentActivityList, [] as SubagentActivity[]);
}

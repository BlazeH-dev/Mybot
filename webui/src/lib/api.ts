import type {
  ChatSummary,
  CliAppsPayload,
  FilePreviewPayload,
  EvaluationCatalogPayload,
  EvaluationCase,
  EvaluationReadiness,
  EvaluationRequestPayload,
  EvaluationRunsPayload,
  ImageGenerationSettingsUpdate,
  McpPresetsPayload,
  ModelConfigurationCreate,
  ModelConfigurationUpdate,
  NetworkSafetySettingsUpdate,
  ObservabilitySettingsUpdate,
  ProviderModelsPayload,
  ProviderSettingsUpdate,
  SessionAutomationsPayload,
  SettingsPayload,
  SettingsUpdate,
  SidebarStatePayload,
  SkillDetail,
  SkillsPayload,
  SlashCommand,
  TranscriptionSettingsUpdate,
  TurnTracePayload,
  WebSearchSettingsUpdate,
  WorkspacesPayload,
  WorkspaceDirectoriesPayload,
  WebuiThreadPersistedPayload,
  WorkspaceScopePayload,
} from "./types";
import { fetchWithTimeout } from "./http";

const API_READ_TIMEOUT_MS = 20_000;
const EVALUATION_READ_TIMEOUT_MS = 60_000;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
  timeoutMs: number = 0,
): Promise<T> {
  const res = await fetchWithTimeout(
    url,
    {
      ...(init ?? {}),
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      credentials: "same-origin",
    },
    timeoutMs,
  );
  if (!res.ok) {
    const text = typeof res.text === "function" ? (await res.text()).trim() : "";
    let detail = text;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
      else if (Array.isArray(parsed.detail)) {
        detail = parsed.detail
          .map((item) => typeof item?.msg === "string" ? item.msg : "Invalid request")
          .join("; ");
      }
    } catch {
      // Preserve plain-text errors from compatibility endpoints.
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  const contentType = res.headers?.get?.("content-type") ?? "";
  if (contentType && !contentType.toLowerCase().includes("application/json")) {
    const text = typeof res.text === "function" ? await res.text() : "";
    const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype");
    throw new ApiError(
      res.status,
      isHtml
        ? "Gateway returned WebUI HTML instead of JSON. Restart nanobot gateway and try again."
        : "Gateway returned a non-JSON response.",
    );
  }
  return (await res.json()) as T;
}

function jsonInit(method: "POST" | "PUT" | "PATCH" | "DELETE", body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    run_started_at?: number | null;
    workspace_scope?: WorkspaceScopePayload | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    runStartedAt: s.run_started_at ?? null,
    workspaceScope: s.workspace_scope ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  const res = await fetchWithTimeout(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function fetchTurnTrace(
  token: string,
  key: string,
  turnId: string,
  base: string = "",
): Promise<TurnTracePayload> {
  const query = new URLSearchParams({ turn_id: turnId });
  return request<TurnTracePayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/trace?${query}`,
    token,
    { cache: "no-store" },
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchFilePreview(
  token: string,
  key: string,
  path: string,
  base: string = "",
): Promise<FilePreviewPayload> {
  const query = new URLSearchParams();
  query.set("path", path);
  return request<FilePreviewPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/file-preview?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSessionAutomations(
  token: string,
  key: string,
  base: string = "",
): Promise<SessionAutomationsPayload> {
  return request<SessionAutomationsPayload>(
    `${base}/api/sessions/${encodeURIComponent(key)}/automations`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/webui/skills`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchEvaluationCatalog(
  token: string,
  base: string = "",
): Promise<EvaluationCatalogPayload> {
  return request<EvaluationCatalogPayload>(
    `${base}/api/evaluations/catalog`,
    token,
    undefined,
    EVALUATION_READ_TIMEOUT_MS,
  );
}

export async function fetchEvaluationReadiness(
  token: string,
  evaluation: EvaluationRequestPayload,
  base: string = "",
): Promise<EvaluationReadiness> {
  const query = new URLSearchParams();
  query.set("suite_id", evaluation.suite_id);
  query.set("profile", evaluation.profile);
  query.set("action", evaluation.action);
  query.set("benchmarks", evaluation.benchmarks.join(","));
  query.set("skills", evaluation.skills.join(","));
  query.set("model_presets", evaluation.model_presets.join(","));
  query.set("runtime_profiles", evaluation.runtime_profiles.join(","));
  query.set("benchmark_samples", JSON.stringify(evaluation.benchmark_samples));
  query.set("allow_licensed_content", String(evaluation.allow_licensed_content));
  return request<EvaluationReadiness>(
    `${base}/api/evaluations/readiness?${query}`,
    token,
    undefined,
    EVALUATION_READ_TIMEOUT_MS,
  );
}

export async function fetchEvaluationRuns(
  token: string,
  base: string = "",
): Promise<EvaluationRunsPayload> {
  return request<EvaluationRunsPayload>(
    `${base}/api/evaluations/runs`,
    token,
    undefined,
    EVALUATION_READ_TIMEOUT_MS,
  );
}

export async function fetchEvaluationCases(
  token: string,
  runId: string,
  base: string = "",
): Promise<EvaluationCase[]> {
  const payload = await request<{ cases: EvaluationCase[] }>(
    `${base}/api/evaluations/runs/${encodeURIComponent(runId)}/cases`,
    token,
    undefined,
    EVALUATION_READ_TIMEOUT_MS,
  );
  return payload.cases;
}

export async function fetchSkillEvolutionBadCases(
  token: string,
  runId: string,
  threshold: number,
  base: string = "",
): Promise<{ threshold: number; cases: import("@/lib/types").SkillEvolutionBadCase[] }> {
  const query = new URLSearchParams({ run_id: runId, threshold: String(threshold) });
  return request(`${base}/api/skill-evolution/bad-cases?${query}`, token, undefined, EVALUATION_READ_TIMEOUT_MS);
}

export async function analyzeSkillEvolution(
  token: string,
  runId: string,
  threshold: number,
  sourceModelPreset: string,
  optimizerPreset: string,
  caseIds: string[],
  base: string = "",
): Promise<import("@/lib/types").SkillEvolutionTask> {
  const body = {
    run_id: runId,
    threshold,
    source_model_preset: sourceModelPreset,
    optimizer_preset: optimizerPreset,
    case_ids: caseIds,
  };
  return request(
    `${base}/api/skill-evolution/analyze`,
    token,
    jsonInit("POST", body),
  );
}

export async function fetchSkillEvolutionTask(
  token: string,
  taskId: string,
  base: string = "",
): Promise<import("@/lib/types").SkillEvolutionTask> {
  return request(`${base}/api/skill-evolution/tasks/${encodeURIComponent(taskId)}`, token);
}

export async function fetchSkillEvolutionTasks(
  token: string,
  base: string = "",
): Promise<import("@/lib/types").SkillEvolutionTask[]> {
  const payload = await request<{ tasks: import("@/lib/types").SkillEvolutionTask[] }>(
    `${base}/api/skill-evolution/tasks`,
    token,
  );
  return payload.tasks;
}

export async function fetchSkillEvolutionActivities(
  token: string,
  taskId: string,
  after: number,
  base: string = "",
): Promise<{
  activities: import("@/lib/types").SkillEvolutionActivity[];
  cursor: number;
}> {
  const query = new URLSearchParams({ after: String(after) });
  return request(
    `${base}/api/skill-evolution/tasks/${encodeURIComponent(taskId)}/activities?${query}`,
    token,
  );
}

export async function evolveSkillEvolution(
  token: string,
  taskId: string,
  analysisId: string,
  analysisDigest: string,
  selectionIds: string[],
  selectionType: "categories" | "findings" = "categories",
  base: string = "",
): Promise<import("@/lib/types").SkillEvolutionTask> {
  const body = {
    analysis_id: analysisId,
    analysis_digest: analysisDigest,
    category_ids: selectionType === "categories" ? selectionIds : undefined,
    finding_ids: selectionType === "findings" ? selectionIds : undefined,
  };
  return request(
    `${base}/api/skill-evolution/tasks/${encodeURIComponent(taskId)}/evolve`,
    token,
    jsonInit("POST", body),
  );
}

export async function runSkillEvolutionAction(
  token: string,
  taskId: string,
  action: "reanalyze" | "revise" | "cancel" | "test" | "apply" | "switch-back",
  revisionId: string = "r1",
  selectionIds: string[] = [],
  selectionType: "categories" | "findings" = "categories",
  base: string = "",
): Promise<import("@/lib/types").SkillEvolutionTask> {
  const body = action === "revise"
    ? {
        category_ids: selectionType === "categories" && selectionIds.length ? selectionIds : undefined,
        finding_ids: selectionType === "findings" && selectionIds.length ? selectionIds : undefined,
      }
    : action === "test" || action === "apply"
      ? { revision_id: revisionId }
      : {};
  return request(
    `${base}/api/skill-evolution/tasks/${encodeURIComponent(taskId)}/${action}`,
    token,
    jsonInit("POST", body),
  );
}

export type DeleteEvaluationRunResult = {
  deleted: boolean;
  scheduled?: boolean;
};

export async function deleteEvaluationRun(
  token: string,
  runId: string,
  base: string = "",
): Promise<DeleteEvaluationRunResult> {
  const payload = await request<DeleteEvaluationRunResult>(
    `${base}/api/evaluations/runs/${encodeURIComponent(runId)}`,
    token,
    { ...jsonInit("DELETE"), cache: "no-store" },
    API_READ_TIMEOUT_MS,
  );
  return payload;
}

export async function fetchSkillDetail(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillDetail> {
  return request<SkillDetail>(
    `${base}/api/webui/skills/${encodeURIComponent(name)}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}`,
    token,
    jsonInit("DELETE"),
  );
  return body.deleted;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchSettingsUsage(
  token: string,
  base: string = "",
): Promise<NonNullable<SettingsPayload["usage"]>> {
  return request<NonNullable<SettingsPayload["usage"]>>(
    `${base}/api/settings/usage`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchWorkspaces(
  token: string,
  base: string = "",
): Promise<WorkspacesPayload> {
  return request<WorkspacesPayload>(
    `${base}/api/workspaces`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchWorkspaceDirectories(
  token: string,
  path?: string | null,
  base: string = "",
): Promise<WorkspaceDirectoriesPayload> {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  const suffix = query.size ? `?${query}` : "";
  return request<WorkspaceDirectoriesPayload>(
    `${base}/api/workspaces/directories${suffix}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(
    `${base}/api/settings/cli-apps/${action}`,
    token,
    jsonInit("POST", { name }),
  );
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function fetchProviderModels(
  token: string,
  provider: string,
  base: string = "",
): Promise<ProviderModelsPayload> {
  const query = new URLSearchParams();
  query.set("provider", provider);
  return request<ProviderModelsPayload>(
    `${base}/api/settings/provider-models?${query}`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${action}`,
    token,
    jsonInit("POST", { name, ...values }),
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    jsonInit("POST", values),
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    jsonInit("POST", { config }),
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    jsonInit("POST", { name, enabled_tools: enabledTools }),
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(
    `${base}/api/commands`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state`,
    token,
    undefined,
    API_READ_TIMEOUT_MS,
  );
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state`,
    token,
    jsonInit("PUT", state),
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body: Record<string, unknown> = {};
  if (update.modelPreset !== undefined) {
    body.model_preset = update.modelPreset ?? "default";
  }
  if (update.model !== undefined) body.model = update.model;
  if (update.provider !== undefined) body.provider = update.provider;
  if (update.contextWindowTokens !== undefined) {
    body.context_window_tokens = update.contextWindowTokens;
  }
  if (update.timezone !== undefined) body.timezone = update.timezone;
  if (update.botName !== undefined) body.bot_name = update.botName;
  if (update.botIcon !== undefined) body.bot_icon = update.botIcon;
  if (update.toolHintMaxLength !== undefined) {
    body.tool_hint_max_length = update.toolHintMaxLength;
  }
  if (update.toolMode !== undefined) body.tool_mode = update.toolMode;
  return request<SettingsPayload>(`${base}/api/settings`, token, jsonInit("PATCH", body));
}

export async function updateSkillEnabled(
  token: string,
  name: string,
  enabled: boolean,
  base: string = "",
): Promise<SkillsPayload> {
  return request<SkillsPayload>(
    `${base}/api/settings/skills/${encodeURIComponent(name)}`,
    token,
    jsonInit("PUT", { enabled }),
  );
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const body = {
    name: configuration.name,
    label: configuration.label,
    provider: configuration.provider,
    model: configuration.model,
    context_window_tokens: configuration.contextWindowTokens,
  };
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations`,
    token,
    jsonInit("POST", body),
  );
}

export async function updateModelConfiguration(
  token: string,
  configuration: ModelConfigurationUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body = {
    label: configuration.label,
    provider: configuration.provider,
    model: configuration.model,
    context_window_tokens: configuration.contextWindowTokens,
  };
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/${encodeURIComponent(configuration.name)}`,
    token,
    jsonInit("PATCH", body),
  );
}

export async function deleteModelConfiguration(
  token: string,
  name: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/${encodeURIComponent(name)}`,
    token,
    jsonInit("DELETE"),
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body = {
    provider: update.provider,
    api_key: update.apiKey,
    api_base: update.apiBase,
    api_type: update.apiType,
  };
  return request<SettingsPayload>(
    `${base}/api/settings/provider`,
    token,
    jsonInit("PATCH", body),
  );
}

export async function loginProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth/login`,
    token,
    jsonInit("POST", { provider }),
  );
}

export async function logoutProviderOAuth(
  token: string,
  provider: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/provider/oauth/logout`,
    token,
    jsonInit("POST", { provider }),
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const body = {
    provider: update.provider,
    api_key: update.apiKey,
    base_url: update.baseUrl,
    max_results: update.maxResults,
    timeout: update.timeout,
    use_jina_reader: update.useJinaReader,
  };
  return request<SettingsPayload>(
    `${base}/api/settings/web-search`,
    token,
    jsonInit("PATCH", body),
  );
}

export async function updateNetworkSafetySettings(
  token: string,
  update: NetworkSafetySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/network-safety`,
    token,
    jsonInit("PATCH", {
      webui_allow_local_service_access: update.webuiAllowLocalServiceAccess,
      webui_default_access_mode: update.webuiDefaultAccessMode,
    }),
  );
}

export async function updateObservabilitySettings(
  token: string,
  update: ObservabilitySettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/observability`,
    token,
    jsonInit("PATCH", { langfuse_enabled: update.langfuseEnabled }),
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation`,
    token,
    jsonInit("PATCH", {
      enabled: update.enabled,
      provider: update.provider,
      model: update.model,
      default_aspect_ratio: update.defaultAspectRatio,
      default_image_size: update.defaultImageSize,
      max_images_per_turn: update.maxImagesPerTurn,
    }),
  );
}

export async function updateTranscriptionSettings(
  token: string,
  update: TranscriptionSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(
    `${base}/api/settings/transcription`,
    token,
    jsonInit("PATCH", {
      enabled: update.enabled,
      provider: update.provider,
      model: update.model,
      language: update.language,
      max_duration_sec: update.maxDurationSec,
      max_upload_mb: update.maxUploadMb,
    }),
  );
}

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createModelConfiguration,
  deleteModelConfiguration,
  deleteSession,
  fetchFilePreview,
  fetchCliApps,
  fetchMcpPresets,
  fetchProviderModels,
  fetchSessionAutomations,
  fetchSettingsUsage,
  fetchSidebarState,
  fetchSkillDetail,
  fetchSkills,
  fetchTurnTrace,
  fetchWebuiThread,
  fetchWorkspaceDirectories,
  fetchWorkspaces,
  importMcpConfig,
  listSessions,
  listSlashCommands,
  loginProviderOAuth,
  logoutProviderOAuth,
  runCliAppAction,
  runMcpPresetAction,
  saveCustomMcpServer,
  updateSidebarState,
  updateImageGenerationSettings,
  updateModelConfiguration,
  updateMcpServerTools,
  updateNetworkSafetySettings,
  updateObservabilitySettings,
  updateProviderSettings,
  updateSettings,
  updateSkillEnabled,
  updateWebSearchSettings,
} from "@/lib/api";

describe("webui API helpers", () => {
  function expectJsonCall(
    url: string,
    method: string,
    body?: Record<string, unknown>,
    callIndex: number = -1,
  ) {
    const call = vi.mocked(fetch).mock.calls.at(callIndex);
    expect(call?.[0]).toBe(url);
    expect(call?.[1]).toEqual(expect.objectContaining({
      method,
      headers: {
        Authorization: "Bearer tok",
        "Content-Type": "application/json",
      },
    }));
    if (body !== undefined) {
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject(body);
    }
  }

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ deleted: true, key: "websocket:chat-1", messages: [] }),
      }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("percent-encodes websocket keys when fetching webui-thread snapshot", async () => {
    await fetchWebuiThread("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/webui-thread",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
        credentials: "same-origin",
      }),
    );
  });

  it("percent-encodes websocket keys and paths when fetching file previews", async () => {
    await fetchFilePreview("tok", "websocket:chat-1", "/tmp/project/hook.py:12");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/file-preview?path=%2Ftmp%2Fproject%2Fhook.py%3A12",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
        credentials: "same-origin",
      }),
    );
  });

  it("fetches a trace for one encoded WebUI turn", async () => {
    await fetchTurnTrace("tok", "websocket:chat-1", "turn:1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/trace?turn_id=turn%3A1",
      expect.objectContaining({
        cache: "no-store",
        headers: { Authorization: "Bearer tok" },
        credentials: "same-origin",
      }),
    );
  });

  it("percent-encodes websocket keys when fetching session automations", async () => {
    await fetchSessionAutomations("tok", "websocket:chat-1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/sessions/websocket%3Achat-1/automations",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("fetches the WebUI skill summary", async () => {
    await fetchSkills("tok");

    expect(fetch).toHaveBeenCalledWith(
      "/api/webui/skills",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("percent-encodes skill names when fetching skill details", async () => {
    await fetchSkillDetail("tok", "current web");

    expect(fetch).toHaveBeenCalledWith(
      "/api/webui/skills/current%20web",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("updates a Skill's enabled state", async () => {
    await updateSkillEnabled("tok", "officecli", false);
    expectJsonCall("/api/settings/skills/officecli", "PUT", { enabled: false });
  });

  it("percent-encodes websocket keys when deleting a session", async () => {
    await deleteSession("tok", "websocket:chat-1");

    expectJsonCall("/api/sessions/websocket%3Achat-1", "DELETE");
  });

  it("serializes settings updates as JSON", async () => {
    await updateSettings("tok", {
      modelPreset: "default",
      model: "openrouter/test",
      provider: "openrouter",
      contextWindowTokens: 262144,
      timezone: "Asia/Shanghai",
      botName: "nanobot",
      botIcon: "nb",
      toolHintMaxLength: 120,
    });

    expectJsonCall("/api/settings", "PATCH", {
      model_preset: "default",
      model: "openrouter/test",
      provider: "openrouter",
      context_window_tokens: 262144,
      timezone: "Asia/Shanghai",
      bot_name: "nanobot",
      bot_icon: "nb",
      tool_hint_max_length: 120,
    });
  });

  it("fetches token usage through the lightweight settings endpoint", async () => {
    await fetchSettingsUsage("tok");

    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/usage",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("serializes model configuration creation", async () => {
    await createModelConfiguration("tok", {
      label: "Fast writing",
      provider: "openai",
      model: "openai/gpt-4.1-mini",
      contextWindowTokens: 262144,
    });

    expectJsonCall("/api/settings/model-configurations", "POST", {
      label: "Fast writing",
      provider: "openai",
      model: "openai/gpt-4.1-mini",
      context_window_tokens: 262144,
    });
  });

  it("serializes model configuration deletion", async () => {
    await deleteModelConfiguration("tok", "fast-writing");

    expectJsonCall("/api/settings/model-configurations/fast-writing", "DELETE");
  });

  it("serializes model configuration updates", async () => {
    await updateModelConfiguration("tok", {
      name: "codex",
      label: "Codex",
      provider: "openai_codex",
      model: "openai-codex/gpt-5.5",
      contextWindowTokens: 65536,
    });

    expectJsonCall("/api/settings/model-configurations/codex", "PATCH", {
      label: "Codex",
      provider: "openai_codex",
      model: "openai-codex/gpt-5.5",
      context_window_tokens: 65536,
    });
  });

  it("reports HTML API fallbacks as gateway mismatch errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "text/html; charset=utf-8" }),
        text: async () => "<!doctype html><html></html>",
      }),
    );

    await expect(
      updateModelConfiguration("tok", {
        name: "codex",
        model: "openai-codex/gpt-5.5",
      }),
    ).rejects.toMatchObject({
      status: 200,
      message: "Gateway returned WebUI HTML instead of JSON. Restart nanobot gateway and try again.",
    });
  });

  it("surfaces API error response bodies", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => "npm error ENOTEMPTY",
      }),
    );

    await expect(runCliAppAction("tok", "install", "hyperframes")).rejects.toMatchObject({
      status: 500,
      message: "npm error ENOTEMPTY",
    });
  });

  it("extracts FastAPI detail errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        text: async () => JSON.stringify({ detail: "invalid optimizer preset" }),
      }),
    );
    await expect(runCliAppAction("tok", "test", "missing")).rejects.toMatchObject({
      status: 422,
      message: "invalid optimizer preset",
    });
  });

  it("times out when an API request never responds", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    const pending = expect(listSessions("tok")).rejects.toThrow(
      "Request timed out after 20000ms",
    );
    await vi.advanceTimersByTimeAsync(20_000);

    await pending;
  });

  it("serializes provider settings updates without returning secrets", async () => {
    await updateProviderSettings("tok", {
      provider: "openrouter",
      apiKey: "sk-or-test",
      apiBase: "https://openrouter.ai/api/v1",
    });

    expectJsonCall("/api/settings/provider", "PATCH", {
      provider: "openrouter",
      api_key: "sk-or-test",
      api_base: "https://openrouter.ai/api/v1",
    });
    expect(String(vi.mocked(fetch).mock.calls[0][0])).not.toContain("sk-or-test");
  });

  it("fetches provider model lists", async () => {
    await fetchProviderModels("tok", "deepseek");

    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/provider-models?provider=deepseek",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("serializes provider OAuth login and logout actions", async () => {
    await loginProviderOAuth("tok", "openai_codex");
    expectJsonCall("/api/settings/provider/oauth/login", "POST", {
      provider: "openai_codex",
    });

    await logoutProviderOAuth("tok", "openai_codex");
    expectJsonCall("/api/settings/provider/oauth/logout", "POST", {
      provider: "openai_codex",
    });
  });

  it("serializes web search settings updates", async () => {
    await updateWebSearchSettings("tok", {
      provider: "searxng",
      baseUrl: "https://search.example.com",
      maxResults: 8,
      timeout: 45,
      useJinaReader: false,
    });

    expectJsonCall("/api/settings/web-search", "PATCH", {
      provider: "searxng",
      base_url: "https://search.example.com",
      max_results: 8,
      timeout: 45,
      use_jina_reader: false,
    });
  });

  it("serializes network safety settings updates", async () => {
    await updateNetworkSafetySettings("tok", {
      webuiAllowLocalServiceAccess: false,
      webuiDefaultAccessMode: "full",
    });

    expectJsonCall("/api/settings/network-safety", "PATCH", {
      webui_allow_local_service_access: false,
      webui_default_access_mode: "full",
    });
  });

  it("serializes observability settings updates", async () => {
    await updateObservabilitySettings("tok", { langfuseEnabled: false });

    expectJsonCall("/api/settings/observability", "PATCH", {
      langfuse_enabled: false,
    });
  });

  it("serializes image generation settings updates", async () => {
    await updateImageGenerationSettings("tok", {
      enabled: true,
      provider: "openrouter",
      model: "openai/gpt-5.4-image-2",
      defaultAspectRatio: "16:9",
      defaultImageSize: "2K",
      maxImagesPerTurn: 3,
    });

    expectJsonCall("/api/settings/image-generation", "PATCH", {
      enabled: true,
      provider: "openrouter",
      model: "openai/gpt-5.4-image-2",
      default_aspect_ratio: "16:9",
      default_image_size: "2K",
      max_images_per_turn: 3,
    });
  });

  it("reads CLI Apps catalog and serializes actions", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        apps: [],
        installed_count: 0,
        catalog_updated_at: "2026-04-18",
      }),
    } as Response);

    await expect(fetchCliApps("tok")).resolves.toMatchObject({ apps: [] });
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/cli-apps",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );

    await runCliAppAction("tok", "install", "gimp");
    expectJsonCall("/api/settings/cli-apps/install", "POST", { name: "gimp" });
  });

  it("reads MCP presets and serializes actions", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        presets: [],
        installed_count: 0,
      }),
    } as Response);

    await expect(fetchMcpPresets("tok")).resolves.toMatchObject({ presets: [] });
    expect(fetch).toHaveBeenCalledWith(
      "/api/settings/mcp-presets",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );

    await runMcpPresetAction("tok", "enable", "browserbase", {
      browserbase_api_key: "bb_live_test",
    });
    expectJsonCall("/api/settings/mcp-presets/enable", "POST", {
      name: "browserbase",
      browserbase_api_key: "bb_live_test",
    });
  });

  it("serializes custom MCP, mcp.json import, and tool allowlist actions", async () => {
    await saveCustomMcpServer("tok", {
      name: "docs",
      transport: "stdio",
      command: "npx",
      args: '["-y","docs-mcp"]',
      env: '{"API_KEY":"secret"}',
    });
    expectJsonCall("/api/settings/mcp-presets/custom", "POST", {
      name: "docs",
      transport: "stdio",
      command: "npx",
      args: '["-y","docs-mcp"]',
      env: '{"API_KEY":"secret"}',
    });

    await importMcpConfig("tok", '{"mcpServers":{"docs":{"command":"npx"}}}');
    expectJsonCall("/api/settings/mcp-presets/import", "POST", {
      config: '{"mcpServers":{"docs":{"command":"npx"}}}',
    });

    await updateMcpServerTools("tok", "docs", ["search", "fetch"]);
    expectJsonCall("/api/settings/mcp-presets/tools", "POST", {
      name: "docs",
      enabled_tools: ["search", "fetch"],
    });
  });

  it("reads and writes persisted sidebar state", async () => {
    const state = {
      schema_version: 1,
      pinned_keys: ["websocket:chat-1"],
      archived_keys: ["websocket:old"],
      title_overrides: { "websocket:chat-1": "Release" },
      project_name_overrides: { "/Users/me/nanobot": "Core" },
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "compact" as const,
        show_previews: false,
        show_timestamps: false,
        show_archived: true,
        sort: "updated_desc" as const,
      },
      updated_at: null,
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => state,
    } as Response);

    await expect(fetchSidebarState("tok")).resolves.toEqual(state);
    expect(fetch).toHaveBeenCalledWith(
      "/api/webui/sidebar-state",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );

    await updateSidebarState("tok", state);
    expectJsonCall("/api/webui/sidebar-state", "PUT", {
      pinned_keys: ["websocket:chat-1"],
      title_overrides: { "websocket:chat-1": "Release" },
      project_name_overrides: { "/Users/me/nanobot": "Core" },
    });
  });

  it("fetches workspace project state", async () => {
    const payload = {
      schema_version: 1,
      default_access_mode: "default" as const,
      default_scope: {
        project_path: "/tmp/workspace",
        project_name: "workspace",
        access_mode: "restricted" as const,
        restrict_to_workspace: true,
      },
      controls: {
        can_change_project: true,
        can_use_full_access: true,
      },
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => payload,
    } as Response);

    await expect(fetchWorkspaces("tok")).resolves.toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(
      "/api/workspaces",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("fetches directories for the browser project picker", async () => {
    const payload = {
      path: "/Users/me/projects",
      parent_path: "/Users/me",
      directories: [{ name: "mybot", path: "/Users/me/projects/mybot" }],
      truncated: false,
    };
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => payload,
    } as Response);

    await expect(fetchWorkspaceDirectories("tok", payload.path)).resolves.toEqual(payload);
    expect(fetch).toHaveBeenCalledWith(
      "/api/workspaces/directories?path=%2FUsers%2Fme%2Fprojects",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });

  it("maps generated session titles from the sessions list", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        sessions: [
          {
            key: "websocket:chat-1",
            created_at: "2026-05-01T10:00:00",
            updated_at: "2026-05-01T10:01:00",
            title: "优化 WebUI 标题",
            run_started_at: 1_700_000_000,
          },
        ],
      }),
    } as Response);

    await expect(listSessions("tok")).resolves.toMatchObject([
      {
        key: "websocket:chat-1",
        title: "优化 WebUI 标题",
        preview: "",
        runStartedAt: 1_700_000_000,
      },
    ]);
  });

  it("maps slash command metadata from the commands endpoint", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        commands: [
          {
            command: "/stop",
            title: "Stop current task",
            description: "Cancel the active task.",
            icon: "square",
          },
          {
            command: "/restart",
            title: "Restart nanobot",
            description: "Restart the bot process.",
            icon: "rotate-cw",
          },
          {
            command: "/history",
            title: "Show conversation history",
            description: "Print the last N messages.",
            icon: "history",
            arg_hint: "[n]",
          },
        ],
      }),
    } as Response);

    await expect(listSlashCommands("tok")).resolves.toEqual([
      {
        command: "/history",
        title: "Show conversation history",
        description: "Print the last N messages.",
        icon: "history",
        argHint: "[n]",
      },
    ]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/commands",
      expect.objectContaining({
        headers: { Authorization: "Bearer tok" },
      }),
    );
  });
});

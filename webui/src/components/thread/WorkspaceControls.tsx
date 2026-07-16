import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AlertTriangle, Check, ChevronDown, Folder, FolderOpen, Hand } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import type {
  WorkspaceAccessMode,
  WorkspaceDirectoriesPayload,
  WorkspaceScopePayload,
  WorkspacesPayload,
} from "@/lib/types";
import { fetchWorkspaceDirectories } from "@/lib/api";
import { getHostApi } from "@/lib/runtime";
import { useOptionalClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";
import {
  isAbsoluteWorkspacePath,
  projectNameFromPath,
  scopeWithAccessMode,
  selectedProjectScope,
  shortWorkspacePath,
} from "@/lib/workspace";

export function WorkspaceProjectPicker({
  isHero,
  disabled,
  scope,
  defaultScope,
  controls,
  error,
  onChange,
}: {
  isHero: boolean;
  disabled?: boolean;
  scope: WorkspaceScopePayload | null;
  defaultScope: WorkspaceScopePayload | null;
  controls: WorkspacesPayload["controls"] | null;
  error?: string | null;
  onChange?: (scope: WorkspaceScopePayload) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [pathDraft, setPathDraft] = useState("");
  const [pathError, setPathError] = useState<string | null>(null);
  const [pickingFolder, setPickingFolder] = useState(false);
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const [browseResult, setBrowseResult] = useState<WorkspaceDirectoriesPayload | null>(null);
  const [browsing, setBrowsing] = useState(false);
  const clientContext = useOptionalClient();
  const currentProjectScope = selectedProjectScope(scope, defaultScope);
  const projectLabel = currentProjectScope
    ? currentProjectScope.project_name || projectNameFromPath(currentProjectScope.project_path)
    : t("thread.composer.workspace.projectPlaceholder");
  const visible = isHero
    && !!defaultScope
    && !!onChange
    && controls?.can_change_project !== false;
  const hostApi = getHostApi();
  const nativeProjectPicker = !!hostApi;

  useEffect(() => {
    if (!open) return;
    setPathDraft(currentProjectScope?.project_path ?? "");
    setPathError(null);
  }, [currentProjectScope?.project_path, open]);

  useEffect(() => {
    if (error && visible) setOpen(true);
  }, [error, visible]);

  const applyProjectPath = useCallback(
    (projectPath: string, projectName?: string) => {
      const base = scope ?? defaultScope;
      const trimmed = projectPath.trim();
      if (!base || !onChange) return;
      if (!trimmed || !isAbsoluteWorkspacePath(trimmed)) {
        setPathError(t("workspace.dialog.absolutePathRequired"));
        return;
      }
      onChange({
        ...base,
        project_path: trimmed,
        project_name: projectName || projectNameFromPath(trimmed),
        restrict_to_workspace: base.access_mode === "restricted",
      });
      setPathError(null);
      setBrowsePath(null);
      setBrowseResult(null);
      setOpen(false);
    },
    [defaultScope, onChange, scope, t],
  );

  const pickNativeFolder = useCallback(async () => {
    if (!hostApi || disabled) return;
    setPickingFolder(true);
    try {
      const picked = await hostApi.pickFolder();
      if (picked) applyProjectPath(picked);
    } catch (err) {
      setPathError((err as Error).message);
    } finally {
      setPickingFolder(false);
    }
  }, [applyProjectPath, disabled, hostApi]);

  const browseFolders = useCallback(async (path?: string | null) => {
    if (!clientContext?.token || disabled) return;
    setBrowsing(true);
    setPathError(null);
    try {
      const result = await fetchWorkspaceDirectories(clientContext.token, path);
      setBrowseResult(result);
      setBrowsePath(result.path);
    } catch (err) {
      setPathError((err as Error).message);
    } finally {
      setBrowsing(false);
    }
  }, [clientContext?.token, disabled]);

  if (!visible || !defaultScope || !onChange) return null;

  if (nativeProjectPicker) {
    return (
      <div className="flex items-center rounded-b-[28px] border-t border-border/25 bg-muted/60 px-4 py-1.5 dark:bg-white/[0.055]">
        <button
          type="button"
          disabled={disabled || pickingFolder}
          aria-label={t("thread.composer.workspace.projectAria")}
          title={currentProjectScope?.project_path}
          onClick={() => void pickNativeFolder()}
          className={cn(
            "inline-flex h-7 max-w-[18rem] items-center gap-2 rounded-full px-2.5",
            "text-[12px] font-medium text-muted-foreground/90 transition-colors",
            "hover:bg-background/70 hover:text-foreground disabled:pointer-events-none disabled:opacity-55",
            currentProjectScope && "text-foreground/82",
          )}
        >
          <Folder className={cn("h-3.5 w-3.5 shrink-0", currentProjectScope && "text-primary")} />
          <span className="truncate">{projectLabel}</span>
        </button>
        {pathError || error ? (
          <span role="alert" className="ml-2 truncate text-[11.5px] font-medium text-destructive">
            {pathError ?? error}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex items-center rounded-b-[28px] border-t border-border/25 bg-muted/60 px-4 py-1.5 dark:bg-white/[0.055]">
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={t("thread.composer.workspace.projectAria")}
            className={cn(
              "inline-flex h-7 max-w-[18rem] items-center gap-2 rounded-full px-2.5",
              "text-[12px] font-medium text-muted-foreground/90 transition-colors",
              "hover:bg-background/70 hover:text-foreground disabled:pointer-events-none disabled:opacity-55",
              currentProjectScope && "text-foreground/82",
            )}
          >
            <Folder className={cn("h-3.5 w-3.5 shrink-0", currentProjectScope && "text-primary")} />
            <span className="truncate">{projectLabel}</span>
            <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          side="bottom"
          sideOffset={8}
          className="w-[min(25rem,calc(100vw-2rem))] rounded-[22px]"
        >
          <DropdownMenuItem
            onSelect={() => applyProjectPath(defaultScope.project_path, defaultScope.project_name)}
            className="flex min-h-[48px] cursor-default gap-3 rounded-[16px] px-3 py-2.5 focus:bg-muted/55"
          >
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[12px] bg-muted text-foreground/80">
              <Folder className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] font-semibold text-foreground">
                {t("workspace.dialog.defaultProject")}
              </span>
              <span className="block truncate text-[11.5px] text-muted-foreground">
                {shortWorkspacePath(defaultScope.project_path)}
              </span>
            </span>
            {!currentProjectScope ? <Check className="h-4 w-4 text-foreground/80" /> : null}
          </DropdownMenuItem>
          <div className="my-1 h-px bg-border/45" />
          <div
            className="space-y-1.5 px-1.5 py-1.5"
            onKeyDown={(event) => {
              if (event.key !== "Escape") event.stopPropagation();
            }}
          >
            {clientContext?.token ? (
              <div className="px-0.5 pb-1">
                <Button
                  type="button"
                  variant="outline"
                  disabled={disabled || browsing}
                  onClick={() =>
                    void browseFolders(
                      browsePath ?? currentProjectScope?.project_path ?? defaultScope.project_path,
                    )
                  }
                  className="h-8 w-full justify-start gap-2 rounded-xl px-2.5 text-[12px]"
                >
                  <FolderOpen className="h-3.5 w-3.5" />
                  {t("workspace.dialog.browse")}
                </Button>
              </div>
            ) : null}
            {browseResult ? (
              <div className="mb-1.5 overflow-hidden rounded-xl border border-border/55 bg-background/55">
                <div className="flex items-center gap-1 border-b border-border/45 p-1.5">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={!browseResult.parent_path || browsing}
                    onClick={() => void browseFolders(browseResult.parent_path)}
                    className="h-7 px-2 text-[11.5px]"
                  >
                    {t("workspace.dialog.up")}
                  </Button>
                  <span className="min-w-0 flex-1 truncate px-1 text-[11px] text-muted-foreground" title={browseResult.path}>
                    {shortWorkspacePath(browseResult.path)}
                  </span>
                  <Button
                    type="button"
                    size="sm"
                    disabled={disabled}
                    onClick={() => applyProjectPath(browseResult.path)}
                    className="h-7 px-2 text-[11.5px]"
                  >
                    {t("workspace.dialog.selectFolder")}
                  </Button>
                </div>
                <div className="max-h-44 overflow-y-auto p-1">
                  {browseResult.directories.map((directory) => (
                    <button
                      key={directory.path}
                      type="button"
                      disabled={browsing}
                      onClick={() => void browseFolders(directory.path)}
                      className="flex h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-[12px] hover:bg-muted disabled:opacity-60"
                    >
                      <Folder className="h-3.5 w-3.5 shrink-0 text-primary" />
                      <span className="truncate">{directory.name}</span>
                    </button>
                  ))}
                  {!browseResult.directories.length ? (
                    <p className="px-2 py-3 text-[11.5px] text-muted-foreground">{t("workspace.dialog.empty")}</p>
                  ) : null}
                </div>
                {browseResult.truncated ? (
                  <p className="border-t border-border/45 px-2 py-1.5 text-[10.5px] text-muted-foreground">{t("workspace.dialog.truncated")}</p>
                ) : null}
              </div>
            ) : null}
            <form
              className="flex items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                applyProjectPath(pathDraft);
              }}
            >
              <Input
                value={pathDraft}
                disabled={disabled}
                onChange={(event) => {
                  setPathDraft(event.target.value);
                  setPathError(null);
                }}
                placeholder={t("workspace.dialog.manualPlaceholder")}
                aria-label={t("workspace.dialog.manual")}
                className={cn(
                  "h-9 rounded-full border-border/55 bg-background/80 px-3 text-[12.5px]",
                  "focus-visible:ring-1 focus-visible:ring-foreground/10 focus-visible:ring-offset-0",
                )}
              />
              <Button
                type="submit"
                disabled={disabled || !pathDraft.trim()}
                className="h-9 shrink-0 rounded-full px-3 text-[12px]"
              >
                {t("workspace.dialog.usePath")}
              </Button>
            </form>
            {pathError || error ? (
              <p role="alert" className="px-1 text-[11.5px] font-medium text-destructive">
                {pathError ?? error}
              </p>
            ) : null}
          </div>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

export function WorkspaceAccessMenu({
  scope,
  disabled,
  canUseFullAccess,
  isHero,
  onChange,
}: {
  scope: WorkspaceScopePayload;
  disabled?: boolean;
  canUseFullAccess: boolean;
  isHero: boolean;
  onChange?: (scope: WorkspaceScopePayload) => void;
}) {
  const { t } = useTranslation();
  const mode = scope.access_mode;
  const isFull = mode === "full";

  const setMode = (value: WorkspaceAccessMode) => {
    if (value === "full" && !canUseFullAccess) return;
    if (value === mode) return;
    onChange?.(scopeWithAccessMode(scope, value));
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled || !onChange}>
        <Button
          type="button"
          variant="ghost"
          aria-label={t("thread.composer.workspace.accessAria")}
          className={cn(
            "max-w-[12.5rem] rounded-[10px] border border-transparent font-semibold shadow-none",
            isHero ? "h-8 px-2.5 text-[12px]" : "h-9 px-3 text-[12.5px]",
            isFull
              ? "bg-transparent text-orange-600 hover:bg-orange-500/8 dark:text-orange-300 dark:hover:bg-orange-400/10"
              : "bg-transparent text-muted-foreground hover:bg-foreground/[0.045] hover:text-foreground dark:hover:bg-white/[0.06]",
          )}
        >
          {isFull ? (
            <AlertTriangle className={cn("mr-1.5 shrink-0", isHero ? "h-3.5 w-3.5" : "h-3.5 w-3.5")} />
          ) : (
            <Hand className={cn("mr-1.5 shrink-0", isHero ? "h-3.5 w-3.5" : "h-3.5 w-3.5")} />
          )}
          <span className="truncate">
            {t(isFull ? "thread.composer.workspace.full" : "thread.composer.workspace.default")}
          </span>
          <ChevronDown className={cn("ml-1.5 shrink-0", isHero ? "h-3 w-3" : "h-3 w-3")} />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <AccessMenuItem
          icon={<Hand className="h-4 w-4" />}
          label={t("thread.composer.workspace.default")}
          selected={mode === "restricted"}
          onSelect={() => setMode("restricted")}
        />
        <AccessMenuItem
          icon={<AlertTriangle className="h-4 w-4" />}
          label={t("thread.composer.workspace.full")}
          selected={mode === "full"}
          disabled={!canUseFullAccess}
          warning
          onSelect={() => setMode("full")}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AccessMenuItem({
  icon,
  label,
  selected,
  disabled,
  warning,
  onSelect,
}: {
  icon: ReactNode;
  label: string;
  selected: boolean;
  disabled?: boolean;
  warning?: boolean;
  onSelect: () => void;
}) {
  return (
    <DropdownMenuItem
      disabled={disabled}
      onSelect={onSelect}
      className={cn(
        "flex h-10 items-center gap-3 rounded-xl px-3 text-[13.5px] font-semibold",
        warning && "text-orange-600 focus:text-orange-600 dark:text-orange-300 dark:focus:text-orange-300",
      )}
    >
      <span className="grid h-5 w-5 shrink-0 place-items-center text-current" aria-hidden>
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {selected ? <Check className="h-4 w-4 shrink-0" aria-hidden /> : null}
    </DropdownMenuItem>
  );
}

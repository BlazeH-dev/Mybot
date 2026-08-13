import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCheck,
  CircleDot,
  ExternalLink,
  FilePenLine,
  GitCompareArrows,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  WandSparkles,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ActivityStep } from "@/components/thread/activity/ActivityStep";
import { Button } from "@/components/ui/button";
import {
  analyzeSkillEvolution,
  evolveSkillEvolution,
  fetchEvaluationRuns,
  fetchSettings,
  fetchSkillEvolutionActivities,
  fetchSkillEvolutionBadCases,
  fetchSkillEvolutionTask,
  runSkillEvolutionAction,
} from "@/lib/api";
import type {
  EvaluationJob,
  SkillEvolutionActivity,
  SkillEvolutionBadCase,
  SkillEvolutionTask,
  SettingsPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const DEFAULT_OPTIMIZER_PRESET = "gpt-5-6-sol";
const ACTIVE_STATUSES = new Set(["collecting_evidence", "analyzing", "editing", "testing"]);
const TASK_STORAGE_KEY = "nanobot.skillEvolution.activeTask";

function scoreLabel(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(3) : "-";
}

function usageLabel(usage?: Record<string, number>): string {
  if (!usage) return "";
  const total = usage.total_tokens ?? usage.total ?? usage.tokens;
  return typeof total === "number" ? `${total.toLocaleString()} tokens` : "";
}

export function SkillEvolutionCenter({ hostChromeInset = false }: { hostChromeInset?: boolean }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [runs, setRuns] = useState<EvaluationJob[]>([]);
  const [runId, setRunId] = useState("");
  const [threshold, setThreshold] = useState(0.6);
  const [cases, setCases] = useState<SkillEvolutionBadCase[]>([]);
  const [activeModel, setActiveModel] = useState("");
  const [modelPresets, setModelPresets] = useState<SettingsPayload["model_presets"]>([]);
  const [optimizerPreset, setOptimizerPreset] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectedFindings, setSelectedFindings] = useState<Set<string>>(new Set());
  const [task, setTask] = useState<SkillEvolutionTask | null>(null);
  const [activities, setActivities] = useState<SkillEvolutionActivity[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);
  const activityRef = useRef<HTMLDivElement>(null);
  const followActivityRef = useRef(true);
  const cursorRef = useRef(0);

  const loadRuns = useCallback(async () => {
    const [payload, settings] = await Promise.all([
      fetchEvaluationRuns(token),
      fetchSettings(token),
    ]);
    const eligible = payload.jobs.filter((run) =>
      run.action === "run" && (run.request?.benchmarks.includes("ocb") ?? false),
    );
    setRuns(eligible);
    setRunId((current) => current || eligible[0]?.job_id || "");
    const configuredProviders = new Set(
      settings.providers.filter((provider) => provider.configured).map((provider) => provider.name),
    );
    const availablePresets = settings.model_presets.filter((preset) =>
      preset.name !== "default" && configuredProviders.has(preset.provider),
    );
    setModelPresets(availablePresets);
    setOptimizerPreset((current) => current
      || availablePresets.find((preset) => preset.name === DEFAULT_OPTIMIZER_PRESET)?.name
      || availablePresets[0]?.name
      || "");
  }, [token]);

  const loadCases = useCallback(async () => {
    if (!runId) return;
    setBusy("cases");
    setError(null);
    setResultsLoading(false);
    try {
      const payload = await fetchSkillEvolutionBadCases(token, runId, threshold);
      setCases(payload.cases);
      setActiveModel((current) => {
        const models = Array.from(new Set(payload.cases.map((item) => item.model_preset)));
        return models.includes(current) ? current : (models[0] ?? "");
      });
      setSelected(new Set());
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (message.includes("still loading")) setResultsLoading(true);
      else setError(message);
    } finally {
      setBusy(null);
    }
  }, [runId, threshold, token]);

  useEffect(() => {
    void loadRuns().catch((cause) => setError(String(cause)));
    const storedTask = window.localStorage.getItem(TASK_STORAGE_KEY);
    if (storedTask) {
      void fetchSkillEvolutionTask(token, storedTask).then(setTask).catch(() => {
        window.localStorage.removeItem(TASK_STORAGE_KEY);
      });
    }
  }, [loadRuns, token]);

  useEffect(() => {
    if (runId) void loadCases();
  }, [loadCases, runId]);

  useEffect(() => {
    if (!resultsLoading) return;
    const timer = window.setTimeout(() => void loadCases(), 2000);
    return () => window.clearTimeout(timer);
  }, [loadCases, resultsLoading]);

  useEffect(() => {
    if (!task) return;
    window.localStorage.setItem(TASK_STORAGE_KEY, task.task_id);
    let disposed = false;
    const poll = async () => {
      try {
        const [nextTask, activityPayload] = await Promise.all([
          fetchSkillEvolutionTask(token, task.task_id),
          fetchSkillEvolutionActivities(token, task.task_id, cursorRef.current),
        ]);
        if (disposed) return;
        setTask(nextTask);
        if (activityPayload.activities.length) {
          cursorRef.current = activityPayload.cursor;
          setActivities((current) => [...current, ...activityPayload.activities]);
        }
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
      }
    };
    void poll();
    if (!ACTIVE_STATUSES.has(task.status)) return () => { disposed = true; };
    const timer = window.setInterval(() => void poll(), 1000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [task?.task_id, task?.status, token]);

  useEffect(() => {
    if (!followActivityRef.current || !activityRef.current) return;
    activityRef.current.scrollTop = activityRef.current.scrollHeight;
  }, [activities]);

  const analysis = useMemo(
    () => task?.analyses?.find((item) => item.analysis_id === task.active_analysis_id) ?? null,
    [task],
  );
  const revision = useMemo(
    () => task?.revisions.find((item) => item.revision_id === task.active_revision_id) ?? null,
    [task],
  );

  useEffect(() => {
    if (!analysis) return;
    setSelectedFindings(new Set(
      analysis.findings
        .filter((finding) => finding.fix_owner === "skill"
          && finding.should_modify_skill
          && finding.confidence >= 0.6)
        .map((finding) => finding.finding_id),
    ));
  }, [analysis?.analysis_id]);

  const casesByModel = useMemo(() => {
    const grouped = new Map<string, SkillEvolutionBadCase[]>();
    for (const item of cases) {
      const rows = grouped.get(item.model_preset) ?? [];
      rows.push(item);
      grouped.set(item.model_preset, rows);
    }
    return grouped;
  }, [cases]);
  const activeCases = useMemo(
    () => casesByModel.get(activeModel) ?? [],
    [activeModel, casesByModel],
  );
  const selectableCaseKeys = useMemo(() => activeCases.map((item) => item.case_key), [activeCases]);
  const allSelectableCasesSelected = selectableCaseKeys.length > 0
    && selectableCaseKeys.every((key) => selected.has(key));
  const running = task ? ACTIVE_STATUSES.has(task.status) : false;

  const analyze = async () => {
    setBusy("analyze");
    setError(null);
    try {
      const selectedCaseIds = activeCases
        .filter((item) => selected.has(item.case_key))
        .map((item) => item.case_id);
      const next = await analyzeSkillEvolution(
        token,
        runId,
        threshold,
        activeModel,
        optimizerPreset,
        selectedCaseIds,
      );
      cursorRef.current = 0;
      setActivities([]);
      followActivityRef.current = true;
      setTask(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  };

  const evolve = async () => {
    if (!task || !analysis) return;
    setBusy("evolve");
    setError(null);
    try {
      setTask(await evolveSkillEvolution(
        token,
        task.task_id,
        analysis.analysis_id,
        analysis.digest,
        Array.from(selectedFindings),
      ));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  };

  const act = async (
    action: "reanalyze" | "revise" | "cancel" | "test" | "apply" | "switch-back",
  ) => {
    if (!task) return;
    setBusy(action);
    setError(null);
    try {
      setTask(await runSkillEvolutionAction(
        token,
        task.task_id,
        action,
        revision?.revision_id ?? "r1",
        action === "revise" ? Array.from(selectedFindings) : [],
      ));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background text-foreground">
      <header className={cn("border-b border-border/70 px-5 pb-4", hostChromeInset ? "pt-12" : "pt-5")}>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold tracking-normal">{t("skillEvolution.title")}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{t("skillEvolution.subtitle")}</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void loadCases()} disabled={!runId || busy !== null}>
            <RefreshCw className="mr-2 h-4 w-4" />
            {t("skillEvolution.refresh")}
          </Button>
        </div>
        <div className="mt-4 flex flex-wrap items-end gap-3">
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("skillEvolution.baselineRun")}
            <select aria-label={t("skillEvolution.baselineRun")} value={runId} onChange={(event) => setRunId(event.target.value)} className="h-9 min-w-72 rounded-md border border-input bg-background px-3 text-sm text-foreground">
              {runs.map((run) => <option key={run.job_id} value={run.job_id}>{run.profile} · {run.job_id} · {run.status}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("skillEvolution.threshold")}
            <input aria-label={t("skillEvolution.threshold")} type="number" min="0" max="1" step="0.05" value={threshold} onChange={(event) => setThreshold(Number(event.target.value))} className="h-9 w-28 rounded-md border border-input bg-background px-3 text-sm text-foreground" />
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("skillEvolution.optimizerModel")}
            <select aria-label={t("skillEvolution.optimizerModel")} value={optimizerPreset} onChange={(event) => setOptimizerPreset(event.target.value)} className="h-9 min-w-48 rounded-md border border-input bg-background px-3 text-sm text-foreground">
              {modelPresets.map((preset) => <option key={preset.name} value={preset.name}>{preset.label}</option>)}
            </select>
          </label>
          <Button onClick={() => void analyze()} disabled={!selected.size || !optimizerPreset || busy !== null || running}>
            {busy === "analyze" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
            {t("skillEvolution.analyze", { count: selected.size })}
          </Button>
          {running ? (
            <Button variant="outline" onClick={() => void act("cancel")} disabled={busy !== null}>
              <Square className="mr-2 h-4 w-4" />{t("skillEvolution.cancel")}
            </Button>
          ) : null}
        </div>
        {error || task?.error ? <p role="alert" className="mt-3 text-sm text-destructive">{error || task?.error}</p> : null}
      </header>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(390px,0.8fr)_minmax(560px,1.2fr)]">
        <div className="min-h-0 overflow-auto border-r border-border/70">
          {casesByModel.size > 0 ? (
            <div className="flex gap-1 overflow-x-auto border-b border-border/70 px-5 pt-3" role="tablist" aria-label={t("skillEvolution.evaluationModel")}>
              {Array.from(casesByModel.entries()).map(([model, rows]) => (
                <button key={model} type="button" role="tab" aria-selected={activeModel === model} onClick={() => { setActiveModel(model); setSelected(new Set()); }} className={cn("whitespace-nowrap border-b-2 px-3 py-2 text-sm", activeModel === model ? "border-foreground font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}>
                  {modelPresets.find((preset) => preset.name === model)?.label ?? model} · {rows.length}
                </button>
              ))}
            </div>
          ) : null}
          <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border/70 bg-background/95 px-5 py-3 backdrop-blur">
            <div className="text-sm font-medium">{t("skillEvolution.badCases")}</div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{selected.size}/{activeCases.length}</span>
              <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={!activeCases.length || busy === "cases" || resultsLoading} onClick={() => setSelected(allSelectableCasesSelected ? new Set() : new Set(selectableCaseKeys))}>
                <CheckCheck className="mr-1.5 h-3.5 w-3.5" />
                {t(allSelectableCasesSelected ? "skillEvolution.deselectAll" : "skillEvolution.selectAll")}
              </Button>
            </div>
          </div>
          {busy === "cases" || resultsLoading ? (
            <div className="flex h-40 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />{t("skillEvolution.loading")}</div>
          ) : activeCases.length ? (
            <table className="w-full table-fixed text-left text-sm">
              <thead className="border-b border-border/70 text-xs text-muted-foreground"><tr><th className="w-11 px-4 py-2" /><th className="w-24 px-2 py-2">Case</th><th className="w-24 px-4 py-2 text-right">{t("skillEvolution.score")}</th></tr></thead>
              <tbody>{activeCases.map((item) => (
                <tr key={item.case_key} className="border-b border-border/50 hover:bg-muted/35">
                  <td className="px-4 py-3"><input type="checkbox" aria-label={`Case ${item.case_id}`} checked={selected.has(item.case_key)} onChange={() => setSelected((current) => { const next = new Set(current); if (next.has(item.case_key)) next.delete(item.case_key); else next.add(item.case_key); return next; })} /></td>
                  <td className="px-2 py-3 font-mono text-xs">{item.case_id}</td>
                  <td className="px-4 py-3 text-right font-mono text-xs">{scoreLabel(item.score)}</td>
                </tr>
              ))}</tbody>
            </table>
          ) : <div className="px-5 py-12 text-center text-sm text-muted-foreground">{t("skillEvolution.noBadCases")}</div>}
        </div>

        <div className="min-h-0 overflow-auto">
          {!task ? (
            <div className="flex h-full min-h-64 items-center justify-center px-8 text-center text-sm text-muted-foreground">{t("skillEvolution.emptyAnalysis")}</div>
          ) : (
            <div className="divide-y divide-border/70">
              <section className="px-5 py-4">
                <div className="flex items-center justify-between gap-3">
                  <div><p className="text-xs uppercase text-muted-foreground">{task.derived_skill} · {task.optimizer_model}</p><h2 className="mt-1 text-base font-semibold">{t("skillEvolution.activity")}</h2></div>
                  <span className="rounded-md border border-border px-2 py-1 text-xs">{task.status}</span>
                </div>
                <div ref={activityRef} onScroll={(event) => { const node = event.currentTarget; followActivityRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 36; }} className="mt-3 max-h-60 overflow-auto border-l border-border/70 pl-3" aria-label={t("skillEvolution.activity")}>
                  {activities.length ? activities.map((activity) => (
                    <ActivityStep key={activity.seq} icon={activity.status === "failed" ? AlertCircle : activity.kind === "file" ? FilePenLine : CircleDot} label={activity.label} detail={activity.detail} active={["started", "running"].includes(activity.status)} tone={activity.status === "failed" ? "error" : activity.status === "completed" ? "success" : "neutral"} aside={<span className="text-[11px] text-muted-foreground">{activity.filePath || activity.caseId || usageLabel(activity.usage)}</span>}>
                      {activity.traceUrl ? <a className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" href={activity.traceUrl} target="_blank" rel="noreferrer">Trace <ExternalLink className="h-3 w-3" /></a> : null}
                    </ActivityStep>
                  )) : <p className="py-3 text-xs text-muted-foreground">{t("skillEvolution.waitingActivity")}</p>}
                </div>
              </section>

              {analysis ? (
                <section className="px-5 py-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><h2 className="text-base font-semibold">{t("skillEvolution.analysis")}</h2><p className="mt-1 text-sm text-muted-foreground">{analysis.summary}</p></div>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => void act("reanalyze")} disabled={busy !== null || running}><RefreshCw className="mr-2 h-4 w-4" />{t("skillEvolution.reanalyze")}</Button>
                      <Button size="sm" onClick={() => void evolve()} disabled={!selectedFindings.size || busy !== null || running}><WandSparkles className="mr-2 h-4 w-4" />{t("skillEvolution.evolve", { count: selectedFindings.size })}</Button>
                    </div>
                  </div>
                  <div className="mt-4 grid gap-3">
                    {analysis.findings.map((finding) => {
                      const selectable = finding.fix_owner === "skill" || finding.fix_owner === "mixed";
                      return (
                        <label key={finding.finding_id} className={cn("grid grid-cols-[20px_minmax(0,1fr)] gap-3 border-l-2 py-2 pl-3", selectable ? "border-foreground/30" : "border-border text-muted-foreground")}>
                          <input type="checkbox" aria-label={`Finding ${finding.finding_id}`} checked={selectedFindings.has(finding.finding_id)} disabled={!selectable || running} onChange={() => setSelectedFindings((current) => { const next = new Set(current); if (next.has(finding.finding_id)) next.delete(finding.finding_id); else next.add(finding.finding_id); return next; })} />
                          <span className="min-w-0">
                            <span className="flex flex-wrap items-center gap-2"><strong className="text-sm">{finding.root_cause}</strong><span className="rounded border border-border px-1.5 py-0.5 text-[11px]">{finding.fix_owner}</span><span className="text-xs text-muted-foreground">{Math.round(finding.confidence * 100)}%</span></span>
                            <span className="mt-1 block text-sm">{finding.change_hypothesis || finding.skill_gap}</span>
                            <span className="mt-1 block text-xs text-muted-foreground">Evidence: {finding.evidence_refs.join(", ")} · Cases: {finding.case_ids.join(", ")}</span>
                            {finding.risk ? <span className="mt-1 block text-xs text-amber-700 dark:text-amber-400">{t("skillEvolution.risk")}: {finding.risk}</span> : null}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                  {analysis.case_observations?.length ? (
                    <div className="mt-4 overflow-hidden rounded-md border border-border">
                      {analysis.case_observations.map((item) => (
                        <div key={item.evidence_id} className="flex flex-wrap items-center gap-3 border-b border-border/60 px-3 py-2 text-xs last:border-b-0">
                          <span className="font-mono">Case {item.case_id}</span>
                          <span className="text-muted-foreground">{item.evidence_id}</span>
                          <span>{item.resource_comparison.peer_count ?? 0} high-score peers</span>
                          {item.resource_comparison.tokens ? <span>tokens {item.resource_comparison.tokens.case} / {item.resource_comparison.tokens.high_score_median} ({item.resource_comparison.tokens.ratio ?? "-"}x)</span> : null}
                          {item.resource_comparison.latency_ms ? <span>latency {item.resource_comparison.latency_ms.case} / {item.resource_comparison.latency_ms.high_score_median} ms ({item.resource_comparison.latency_ms.ratio ?? "-"}x)</span> : null}
                          {item.trace_url ? <a className="ml-auto inline-flex items-center gap-1 text-muted-foreground hover:text-foreground" href={item.trace_url} target="_blank" rel="noreferrer">Trace <ExternalLink className="h-3 w-3" /></a> : null}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </section>
              ) : null}

              {revision ? (
                <>
                  <section className="px-5 py-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div><h2 className="text-base font-semibold">{revision.summary}</h2>{revision.rationale ? <p className="mt-1 text-sm text-muted-foreground">{revision.rationale}</p> : null}</div>
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="outline" onClick={() => void act("revise")} disabled={busy !== null || running}><FilePenLine className="mr-2 h-4 w-4" />{t("skillEvolution.revise")}</Button>
                        <Button size="sm" variant="outline" onClick={() => void act("test")} disabled={busy !== null || running}><Play className="mr-2 h-4 w-4" />{t("skillEvolution.test")}</Button>
                        <Button size="sm" onClick={() => void act("apply")} disabled={busy !== null || revision.status !== "tested" || !revision.recommendation?.recommended}><Check className="mr-2 h-4 w-4" />{t("skillEvolution.apply")}</Button>
                        <Button size="sm" variant="ghost" onClick={() => void act("switch-back")} disabled={busy !== null || running}><RotateCcw className="mr-2 h-4 w-4" />{t("skillEvolution.switchBack")}</Button>
                      </div>
                    </div>
                  </section>
                  <section className="px-5 py-4">
                    <div className="mb-3 flex items-center gap-2 text-sm font-medium"><GitCompareArrows className="h-4 w-4" />{t("skillEvolution.changes")}</div>
                    <div className="mb-3 flex flex-wrap gap-1.5">{revision.changed_paths.map((path) => <span key={path} className="rounded-md bg-muted px-2 py-1 font-mono text-xs">{path}</span>)}</div>
                    <pre className="max-h-96 overflow-auto rounded-md border border-border bg-muted/35 p-3 text-xs leading-5">{revision.diff}</pre>
                  </section>
                  {revision.test_results.length ? (
                    <section className="px-5 py-4">
                      <h3 className="text-sm font-medium">{t("skillEvolution.testResults")}</h3>
                      <div className="mt-3 overflow-hidden rounded-md border border-border">{revision.test_results.map((result) => <div key={result.case_key} className="grid grid-cols-[70px_1fr_72px_72px_60px] items-center gap-2 border-b border-border/60 px-3 py-2 text-xs last:border-b-0"><span className="font-mono">{result.case_id}</span><span className="truncate">{result.model_preset}<span className="ml-1 text-muted-foreground">{result.scope ?? "selected"}</span></span><span className="text-right font-mono">{scoreLabel(result.baseline_score)}</span><span className="text-right font-mono">{scoreLabel(result.evolved_score)}</span><span className={cn("text-right font-mono", (result.delta ?? 0) < 0 ? "text-destructive" : "text-emerald-600")}>{result.delta == null ? "-" : `${result.delta >= 0 ? "+" : ""}${result.delta.toFixed(3)}`}</span>{result.trace_url ? <a className="col-span-5 inline-flex items-center gap-1 text-muted-foreground hover:text-foreground" href={result.trace_url} target="_blank" rel="noreferrer">Trace <ExternalLink className="h-3 w-3" /></a> : null}</div>)}</div>
                      {revision.recommendation ? <p className={cn("mt-3 text-sm", revision.recommendation.recommended ? "text-emerald-600" : "text-amber-600")}>{revision.recommendation.recommended ? t("skillEvolution.recommended") : t("skillEvolution.notRecommended")}<span className="ml-2 text-muted-foreground">{revision.recommendation.disclaimer}</span>{revision.recommendation.mean_token_change != null ? <span className="ml-2 text-muted-foreground">{t("skillEvolution.tokenChange", { value: revision.recommendation.mean_token_change })}</span> : null}</p> : null}
                    </section>
                  ) : null}
                </>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

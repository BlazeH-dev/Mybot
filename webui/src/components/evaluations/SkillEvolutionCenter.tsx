import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  CheckCheck,
  ExternalLink,
  GitCompareArrows,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
  WandSparkles,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  fetchEvaluationRuns,
  fetchSkillEvolutionBadCases,
  fetchSkillEvolutionTask,
  generateSkillEvolution,
  runSkillEvolutionAction,
} from "@/lib/api";
import type {
  EvaluationJob,
  SkillEvolutionBadCase,
  SkillEvolutionTask,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const MAX_SELECTED_CASES = 20;

function scoreLabel(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(3) : "-";
}

export function SkillEvolutionCenter({ hostChromeInset = false }: { hostChromeInset?: boolean }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [runs, setRuns] = useState<EvaluationJob[]>([]);
  const [runId, setRunId] = useState("");
  const [threshold, setThreshold] = useState(0.6);
  const [cases, setCases] = useState<SkillEvolutionBadCase[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [task, setTask] = useState<SkillEvolutionTask | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resultsLoading, setResultsLoading] = useState(false);

  const loadRuns = useCallback(async () => {
    const payload = await fetchEvaluationRuns(token);
    const eligible = payload.jobs.filter((run) =>
      run.action === "run" && (run.request?.benchmarks.includes("ocb") ?? false),
    );
    setRuns(eligible);
    setRunId((current) => current || eligible[0]?.job_id || "");
  }, [token]);

  const loadCases = useCallback(async () => {
    if (!runId) return;
    setBusy("cases");
    setError(null);
    setResultsLoading(false);
    try {
      const payload = await fetchSkillEvolutionBadCases(token, runId, threshold);
      setCases(payload.cases);
      setSelected(new Set());
      setTask(null);
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
  }, [loadRuns]);

  useEffect(() => {
    if (runId) void loadCases();
  }, [loadCases, runId]);

  useEffect(() => {
    if (!resultsLoading) return;
    const timer = window.setTimeout(() => void loadCases(), 2000);
    return () => window.clearTimeout(timer);
  }, [loadCases, resultsLoading]);

  useEffect(() => {
    if (!task || !["testing", "generating"].includes(task.status)) return;
    const timer = window.setInterval(() => {
      void fetchSkillEvolutionTask(token, task.task_id).then(setTask).catch(() => undefined);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [task, token]);

  const revision = useMemo(
    () => task?.revisions.find((item) => item.revision_id === task.active_revision_id) ?? null,
    [task],
  );
  const selectableCaseKeys = useMemo(
    () => cases.slice(0, MAX_SELECTED_CASES).map((item) => item.case_key),
    [cases],
  );
  const allSelectableCasesSelected = selectableCaseKeys.length > 0
    && selectableCaseKeys.every((key) => selected.has(key));

  const toggleCase = (key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else if (next.size < MAX_SELECTED_CASES) next.add(key);
      return next;
    });
  };

  const generate = async () => {
    setBusy("generate");
    setError(null);
    try {
      setTask(await generateSkillEvolution(token, runId, threshold, Array.from(selected)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(null);
    }
  };

  const act = async (action: "revise" | "test" | "apply" | "switch-back") => {
    if (!task || !revision) return;
    setBusy(action);
    setError(null);
    try {
      setTask(await runSkillEvolutionAction(token, task.task_id, action, revision.revision_id));
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
            <select
              aria-label={t("skillEvolution.baselineRun")}
              value={runId}
              onChange={(event) => setRunId(event.target.value)}
              className="h-9 min-w-72 rounded-md border border-input bg-background px-3 text-sm text-foreground"
            >
              {runs.map((run) => (
                <option key={run.job_id} value={run.job_id}>
                  {run.profile} · {run.job_id} · {run.status}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 text-xs text-muted-foreground">
            {t("skillEvolution.threshold")}
            <input
              aria-label={t("skillEvolution.threshold")}
              type="number"
              min="0"
              max="1"
              step="0.05"
              value={threshold}
              onChange={(event) => setThreshold(Number(event.target.value))}
              className="h-9 w-28 rounded-md border border-input bg-background px-3 text-sm text-foreground"
            />
          </label>
          <Button onClick={() => void generate()} disabled={!selected.size || busy !== null}>
            {busy === "generate" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}
            {t("skillEvolution.improve", { count: selected.size })}
          </Button>
        </div>
        {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : null}
      </header>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(480px,1.05fr)_minmax(420px,0.95fr)]">
        <div className="min-h-0 overflow-auto border-r border-border/70">
          <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border/70 bg-background/95 px-5 py-3 backdrop-blur">
            <div className="text-sm font-medium">{t("skillEvolution.badCases")}</div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{selected.size}/{cases.length}</span>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-xs"
                disabled={!cases.length || busy === "cases" || resultsLoading}
                onClick={() => setSelected(
                  allSelectableCasesSelected ? new Set() : new Set(selectableCaseKeys),
                )}
              >
                <CheckCheck className="mr-1.5 h-3.5 w-3.5" />
                {t(allSelectableCasesSelected
                  ? "skillEvolution.deselectAll"
                  : "skillEvolution.selectAll")}
              </Button>
            </div>
          </div>
          {busy === "cases" || resultsLoading ? (
            <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {t("skillEvolution.loading")}
            </div>
          ) : cases.length ? (
            <table className="w-full table-fixed text-left text-sm">
              <thead className="border-b border-border/70 text-xs text-muted-foreground">
                <tr>
                  <th className="w-11 px-4 py-2" />
                  <th className="w-24 px-2 py-2">Case</th>
                  <th className="px-2 py-2">{t("skillEvolution.model")}</th>
                  <th className="w-24 px-4 py-2 text-right">{t("skillEvolution.score")}</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((item) => (
                  <tr key={item.case_key} className="border-b border-border/50 hover:bg-muted/35">
                    <td className="px-4 py-3">
                      <input
                        type="checkbox"
                        aria-label={`Case ${item.case_id}`}
                        checked={selected.has(item.case_key)}
                        disabled={!selected.has(item.case_key) && selected.size >= MAX_SELECTED_CASES}
                        onChange={() => toggleCase(item.case_key)}
                      />
                    </td>
                    <td className="px-2 py-3 font-mono text-xs">{item.case_id}</td>
                    <td className="truncate px-2 py-3">{item.model_preset}</td>
                    <td className="px-4 py-3 text-right font-mono text-xs">{scoreLabel(item.score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="px-5 py-12 text-center text-sm text-muted-foreground">{t("skillEvolution.noBadCases")}</div>
          )}
        </div>

        <div className="min-h-0 overflow-auto">
          {!task || !revision ? (
            <div className="flex h-full min-h-64 items-center justify-center px-8 text-center text-sm text-muted-foreground">
              {t("skillEvolution.emptyRevision")}
            </div>
          ) : (
            <div className="divide-y divide-border/70">
              <section className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs uppercase text-muted-foreground">{task.derived_skill} · {task.optimizer_model}</p>
                    <h2 className="mt-1 text-base font-semibold">{revision.summary}</h2>
                    {revision.rationale ? <p className="mt-2 text-sm text-muted-foreground">{revision.rationale}</p> : null}
                  </div>
                  <span className="rounded-md border border-border px-2 py-1 text-xs">{revision.status}</span>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" onClick={() => void act("revise")} disabled={busy !== null || revision.status === "testing"}>
                    <WandSparkles className="mr-2 h-4 w-4" />
                    {t("skillEvolution.revise")}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => void act("test")} disabled={busy !== null || revision.status === "testing"}>
                    {revision.status === "testing" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                    {t("skillEvolution.test")}
                  </Button>
                  <Button size="sm" onClick={() => void act("apply")} disabled={busy !== null || revision.status === "testing"}>
                    <Check className="mr-2 h-4 w-4" />
                    {t("skillEvolution.apply")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => void act("switch-back")} disabled={busy !== null}>
                    <RotateCcw className="mr-2 h-4 w-4" />
                    {t("skillEvolution.switchBack")}
                  </Button>
                </div>
                {task.runtime_refresh ? (
                  <p className={cn("mt-3 text-sm", task.runtime_refresh.ok ? "text-emerald-600" : "text-amber-600")}>
                    {task.runtime_refresh.message}
                  </p>
                ) : null}
              </section>

              <section className="px-5 py-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <GitCompareArrows className="h-4 w-4" />
                  {t("skillEvolution.changes")}
                </div>
                <div className="mb-3 flex flex-wrap gap-1.5">
                  {revision.changed_paths.map((path) => (
                    <span key={path} className="rounded-md bg-muted px-2 py-1 font-mono text-xs">{path}</span>
                  ))}
                </div>
                <pre className="max-h-96 overflow-auto rounded-md border border-border bg-muted/35 p-3 text-xs leading-5">{revision.diff}</pre>
              </section>

              {revision.test_results.length ? (
                <section className="px-5 py-4">
                  <h3 className="text-sm font-medium">{t("skillEvolution.testResults")}</h3>
                  <div className="mt-3 overflow-hidden rounded-md border border-border">
                    {revision.test_results.map((result) => (
                      <div key={result.case_key} className="grid grid-cols-[70px_1fr_72px_72px_60px] items-center gap-2 border-b border-border/60 px-3 py-2 text-xs last:border-b-0">
                        <span className="font-mono">{result.case_id}</span>
                        <span className="truncate">{result.model_preset}</span>
                        <span className="text-right font-mono">{scoreLabel(result.baseline_score)}</span>
                        <span className="text-right font-mono">{scoreLabel(result.evolved_score)}</span>
                        <span className={cn("text-right font-mono", (result.delta ?? 0) < 0 ? "text-destructive" : "text-emerald-600")}>
                          {result.delta == null ? "-" : `${result.delta >= 0 ? "+" : ""}${result.delta.toFixed(3)}`}
                        </span>
                        {result.trace_url ? (
                          <a className="col-span-5 inline-flex items-center gap-1 text-muted-foreground hover:text-foreground" href={result.trace_url} target="_blank" rel="noreferrer">
                            Trace <ExternalLink className="h-3 w-3" />
                          </a>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {revision.recommendation ? (
                    <p className={cn("mt-3 text-sm", revision.recommendation.recommended ? "text-emerald-600" : "text-amber-600")}>
                      {revision.recommendation.recommended ? t("skillEvolution.recommended") : t("skillEvolution.notRecommended")}
                      <span className="ml-2 text-muted-foreground">{revision.recommendation.disclaimer}</span>
                    </p>
                  ) : null}
                </section>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

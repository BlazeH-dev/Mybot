import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleGauge,
  ExternalLink,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  deleteEvaluationRun,
  fetchEvaluationCases,
  fetchEvaluationCatalog,
  fetchEvaluationReadiness,
  fetchEvaluationRuns,
} from "@/lib/api";
import type {
  EvaluationCase,
  EvaluationFailure,
  EvaluationJob,
  EvaluationJobStatus,
  EvaluationMetrics,
  EvaluationOption,
  EvaluationReadiness,
  EvaluationRequestPayload,
  EvaluationRunsPayload,
  EvaluationSuite,
  EvaluationUsage,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const ACTIVE_STATUSES = new Set<EvaluationJobStatus>([
  "queued",
  "preflight",
  "preparing",
  "estimating",
  "running",
  "remote_scoring",
]);

function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat().format(value ?? 0);
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatScore(value: unknown): string {
  if (typeof value === "number") return value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  if (typeof value === "boolean") return value ? "1" : "0";
  return value == null ? "-" : String(value);
}

function displayScoreEntries(scores: Record<string, unknown>): Array<[string, unknown]> {
  return Object.entries(scores).filter(([, value]) => (
    typeof value !== "string" || value.length <= 80
  ));
}

type CaseVariantField = "model_preset" | "skill" | "benchmark";

const CASE_VARIANT_FIELDS: CaseVariantField[] = ["model_preset", "skill", "benchmark"];

function caseVariantFields(cases: EvaluationCase[]): CaseVariantField[] {
  const compared = CASE_VARIANT_FIELDS.filter((field) => (
    new Set(cases.map((item) => item[field]).filter(Boolean)).size > 1
  ));
  if (compared.length > 0) return compared;
  const available = CASE_VARIANT_FIELDS.find((field) => cases.some((item) => item[field]));
  return available ? [available] : [];
}

function formatModelVariant(value: string): string {
  const gpt = /^gpt-(\d+)-(\d+)-(.+)$/.exec(value);
  if (gpt) return `gpt-${gpt[1]}.${gpt[2]}-${gpt[3]}`;
  if (value.toLowerCase().startsWith("deepseek-")) {
    return `DeepSeek-${value.slice("deepseek-".length)}`;
  }
  return value;
}

function caseVariantLabel(item: EvaluationCase, fields: CaseVariantField[]): string {
  const values = fields.map((field) => {
    const value = item[field];
    return field === "model_preset" && value ? formatModelVariant(value) : value;
  });
  return values.filter(Boolean).join(" / ") || "-";
}

function formatDuration(value: number | undefined): string {
  if (value == null || !Number.isFinite(value)) return "-";
  return value < 10 ? `${value.toFixed(1)}s` : `${Math.round(value)}s`;
}

function UsageSummary({ usage, metrics }: { usage?: EvaluationUsage | null; metrics?: EvaluationMetrics | null }) {
  if (!usage && !metrics) return <span className="text-muted-foreground">-</span>;
  return (
    <div className="space-y-0.5 whitespace-nowrap">
      <div>{usage ? <><span className="text-muted-foreground">tokens</span> {formatNumber(usage.total_tokens)}</> : <span className="text-muted-foreground">tokens unavailable</span>}</div>
      {usage ? <div className="text-muted-foreground">in {formatNumber(usage.input_tokens)} · out {formatNumber(usage.output_tokens)}</div> : null}
      {usage?.cached_input_tokens ? <div className="text-muted-foreground">cache {formatNumber(usage.cached_input_tokens)}</div> : null}
      {metrics ? <div className="text-muted-foreground">{metrics.generation_count ?? 0} calls · {formatDuration(metrics.latency_seconds)}{metrics.ttft_seconds != null ? ` · TTFT ${formatDuration(metrics.ttft_seconds)}` : ""}</div> : null}
    </div>
  );
}

function statusTone(status: string): string {
  if (["completed", "awaiting_review"].includes(status)) return "text-emerald-700 bg-emerald-500/10 dark:text-emerald-300";
  if (["failed", "interrupted"].includes(status)) return "text-red-700 bg-red-500/10 dark:text-red-300";
  if (status === "cancelled") return "text-muted-foreground bg-muted";
  if (status === "queued") return "text-amber-700 bg-amber-500/10 dark:text-amber-300";
  return "text-sky-700 bg-sky-500/10 dark:text-sky-300";
}

function StatusPill({ status }: { status: string }) {
  return (
    <span className={cn("inline-flex rounded px-2 py-1 text-[11px] font-medium", statusTone(status))}>
      {status.replaceAll("_", " ")}
    </span>
  );
}

function FailureDetailsButton({ failure }: { failure?: EvaluationFailure | null }) {
  const [open, setOpen] = useState(false);
  if (!failure) return null;
  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 text-red-700 hover:text-red-700 dark:text-red-300 dark:hover:text-red-300"
        onClick={() => setOpen(true)}
        title="View failure reason"
        aria-label="View failure reason"
      >
        <AlertTriangle className="h-3.5 w-3.5" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Evaluation failure details</DialogTitle>
            <DialogDescription>{failure.summary}</DialogDescription>
          </DialogHeader>
          <div className="max-h-[60vh] space-y-5 overflow-y-auto pr-1 text-sm">
            <section>
              <div className="text-xs font-medium text-muted-foreground">Primary cause</div>
              <div className="mt-1 font-medium text-red-700 dark:text-red-300">{failure.label}</div>
              <div className="mt-1 text-muted-foreground">
                {failure.retryable === false ? "Fix configuration before retrying." : "This failure can be retried or resumed."}
              </div>
            </section>
            {failure.detail ? (
              <section>
                <div className="text-xs font-medium text-muted-foreground">Original error</div>
                <pre className="mt-2 whitespace-pre-wrap break-words rounded border bg-muted/35 p-3 font-mono text-xs leading-5">{failure.detail}</pre>
              </section>
            ) : null}
            {failure.signals?.length ? (
              <section>
                <div className="text-xs font-medium text-muted-foreground">Concurrent signals</div>
                <div className="mt-2 divide-y rounded border">
                  {failure.signals.map((signal) => (
                    <div key={signal.category} className="grid gap-1 px-3 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto]">
                      <div><div className="font-medium">{signal.label}</div><div className="text-xs text-muted-foreground">{signal.summary}</div></div>
                      <div className="text-xs text-muted-foreground">{signal.count} occurrences</div>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function OptionGroup({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: EvaluationOption[];
  selected: string[];
  onChange: (value: string[]) => void;
}) {
  const toggle = (id: string) => {
    onChange(selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id]);
  };
  return (
    <fieldset className="min-w-0">
      <legend className="mb-2 text-xs font-medium text-muted-foreground">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {options.map((option) => {
          const disabled = option.available === false || option.compatible === false;
          return (
            <label
              key={option.id}
              title={option.reason ?? undefined}
              className={cn(
                "flex h-8 cursor-pointer items-center gap-2 rounded border px-2.5 text-xs transition-colors",
                selected.includes(option.id) && "border-foreground/35 bg-foreground/[0.06] text-foreground",
                disabled && "cursor-not-allowed opacity-45",
              )}
            >
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-foreground"
                checked={selected.includes(option.id)}
                disabled={disabled}
                onChange={() => toggle(option.id)}
              />
              <span className="whitespace-nowrap">{option.label}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function ProgressBar({ completed, total }: { completed: number; total: number }) {
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-label={`${percent}%`}>
      <div className="h-full bg-foreground/70 transition-[width]" style={{ width: `${percent}%` }} />
    </div>
  );
}

export function EvaluationCenter({ hostChromeInset = false }: { hostChromeInset?: boolean }) {
  const { t } = useTranslation();
  const { client, token } = useClient();
  const [suite, setSuite] = useState<EvaluationSuite | null>(null);
  const [profile, setProfile] = useState("office-smoke");
  const [action, setAction] = useState<"run" | "prepare">("run");
  const [benchmarks, setBenchmarks] = useState<string[]>([]);
  const [skills, setSkills] = useState<string[]>([]);
  const [modelPresets, setModelPresets] = useState<string[]>([]);
  const [runtimeProfiles, setRuntimeProfiles] = useState<string[]>([]);
  const [benchmarkSamples, setBenchmarkSamples] = useState<Record<string, number>>({
    ocb: 1018,
  });
  const [allowLicensedContent, setAllowLicensedContent] = useState(false);
  const [readiness, setReadiness] = useState<EvaluationReadiness | null>(null);
  const [profileReadiness, setProfileReadiness] = useState<Record<string, EvaluationReadiness>>({});
  const [runs, setRuns] = useState<EvaluationRunsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [cases, setCases] = useState<Record<string, EvaluationCase[]>>({});
  const [casesLoading, setCasesLoading] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; label: string; remote: boolean } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const request = useMemo<EvaluationRequestPayload>(() => ({
    suite_id: suite?.id ?? "office",
    profile,
    action,
    benchmarks,
    skills,
    model_presets: modelPresets,
    runtime_profiles: runtimeProfiles,
    benchmark_samples: benchmarkSamples,
    allow_licensed_content: allowLicensedContent,
  }), [
    action,
    allowLicensedContent,
    benchmarks,
    modelPresets,
    benchmarkSamples,
    profile,
    runtimeProfiles,
    skills,
    suite?.id,
  ]);

  const refreshRuns = useCallback(async () => {
    try {
      const payload = await fetchEvaluationRuns(token);
      setRuns(payload);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [token]);

  const refreshAll = useCallback(async () => {
    setLoading(true);
    try {
      const [catalog, history] = await Promise.all([
        fetchEvaluationCatalog(token),
        fetchEvaluationRuns(token),
      ]);
      const firstSuite = catalog.suites[0] ?? null;
      setSuite((current) => current ?? firstSuite);
      if (firstSuite && benchmarks.length === 0) {
        setBenchmarks(firstSuite.benchmarks.map((item) => item.id));
        setSkills(firstSuite.skills.filter((item) => item.available !== false && item.compatible !== false).map((item) => item.id));
        setModelPresets(firstSuite.model_presets.map((item) => item.id));
        setRuntimeProfiles(firstSuite.runtime_profiles.slice(0, 1).map((item) => item.id));
      }
      setRuns(history);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setLoading(false);
    }
  }, [benchmarks.length, token]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    if (!suite) return;
    setReadinessLoading(true);
    const timer = window.setTimeout(() => {
      const profiles = suite.profiles.map((item) => item.id);
      Promise.all(profiles.map((profileId) => fetchEvaluationReadiness(token, {
        ...request,
        profile: profileId,
        action: profileId === "ci" ? "run" : request.action,
      })))
        .then((payloads) => {
          const next = Object.fromEntries(profiles.map((profileId, index) => [profileId, payloads[index]]));
          setProfileReadiness(next);
          setReadiness(next[profile] ?? payloads[0] ?? null);
          setError(null);
        })
        .catch((caught) => {
          setReadiness(null);
          setError((caught as Error).message);
        })
        .finally(() => setReadinessLoading(false));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [profile, request, suite, token]);

  useEffect(() => {
    const timer = window.setInterval(() => void refreshRuns(), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshRuns]);

  useEffect(() => client.onEvaluation((event) => {
    if (event.event === "validation_failed") {
      setSubmitting(false);
      setError(event.errors.join("; "));
      return;
    }
    if (event.event === "evaluation_started" || event.event === "evaluation_cancelled" || event.event === "evaluation_resumed" || event.event === "evaluation_case_rerun") {
      setSubmitting(false);
      setConfirmOpen(false);
    }
    if ("job" in event) {
      setRuns((current) => {
        if (!current) return current;
        const jobs = current.jobs.some((job) => job.job_id === event.job.job_id)
          ? current.jobs.map((job) => job.job_id === event.job.job_id ? event.job : job)
          : [event.job, ...current.jobs];
        return { ...current, jobs };
      });
    }
  }), [client]);

  useEffect(() => {
    if (profile === "ci") setAction("run");
  }, [profile]);

  const start = () => {
    setSubmitting(true);
    setError(null);
    client.startEvaluation(request);
  };

  const requestStart = () => {
    const requiresConfirmation = profile !== "ci" || (action === "prepare" && allowLicensedContent);
    if (requiresConfirmation) {
      setConfirmed(false);
      setConfirmOpen(true);
    } else {
      start();
    }
  };

  const toggleCases = async (runId: string) => {
    if (expandedRun === runId) {
      setExpandedRun(null);
      return;
    }
    setExpandedRun(runId);
    if (cases[runId]) return;
    setCasesLoading(runId);
    try {
      const rows = await fetchEvaluationCases(token, runId);
      setCases((current) => ({ ...current, [runId]: rows }));
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setCasesLoading(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setError(null);
    setDeleteError(null);
    try {
      const deletion = await deleteEvaluationRun(token, deleteTarget.id);
      if (!deletion.deleted) throw new Error("Evaluation history was not deleted");
      setRuns((current) => current ? {
        ...current,
        jobs: current.jobs.filter((job) => job.job_id !== deleteTarget.id),
        langfuse: {
          ...current.langfuse,
          runs: current.langfuse.runs.filter((run) => run.dataset_run_id !== deleteTarget.id),
        },
      } : current);
      setCases((current) => {
        const next = { ...current };
        delete next[deleteTarget.id];
        return next;
      });
      if (expandedRun === deleteTarget.id) setExpandedRun(null);
      setDeleteTarget(null);
    } catch (caught) {
      const message = (caught as Error).message;
      setError(message);
      setDeleteError(message);
    } finally {
      setDeleting(false);
    }
  };

  const currentJobs = (runs?.jobs ?? []).filter((job) => ACTIVE_STATUSES.has(job.status));
  const historicalJobs = (runs?.jobs ?? []).filter((job) => !ACTIVE_STATUSES.has(job.status));
  const totalTokens = readiness?.estimate.estimated_tokens?.total ?? 0;
  const selectionValid = profile === "ci" || (
    benchmarks.length > 0 && skills.length > 0 && modelPresets.length > 0 && runtimeProfiles.length > 0
  );
  const canStart = Boolean(readiness?.ready && selectionValid && !readinessLoading && !submitting);
  return (
    <div className="h-full overflow-y-auto bg-background">
      <div className={cn("mx-auto w-full max-w-[1500px] px-5 pb-12 lg:px-8", hostChromeInset ? "pt-14" : "pt-7")}>
        <header className="flex flex-wrap items-start justify-between gap-4 border-b pb-6">
          <div>
            <div className="flex items-center gap-2">
              <CircleGauge className="h-5 w-5 text-muted-foreground" />
              <h1 className="text-xl font-semibold">{t("evaluations.title", { defaultValue: "Evaluations" })}</h1>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {t("evaluations.subtitle", { defaultValue: "Benchmark runs, scoring progress, and Langfuse handoff" })}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void refreshAll()} disabled={loading}>
            <RefreshCw className={cn("mr-2 h-3.5 w-3.5", loading && "animate-spin")} />
            {t("evaluations.refresh", { defaultValue: "Refresh" })}
          </Button>
        </header>

        <section className="grid gap-3 border-b py-5 sm:grid-cols-2 xl:grid-cols-4" aria-label="readiness">
          {(["ci", "office-smoke", "office-release"] as const).map((profileId) => (
            <ReadinessItem key={profileId} label={profileId} ready={profileReadiness[profileId]?.ready} detail={profileId === "ci" ? "54-case offline baseline" : readinessLoading ? "Checking..." : `${profileReadiness[profileId]?.blockers.length ?? 0} blockers`} />
          ))}
          <ReadinessItem
            label="Langfuse Project"
            ready={runs?.langfuse.refreshing && !runs.langfuse.available ? undefined : runs?.langfuse.available}
            detail={runs?.langfuse.error ?? (runs?.langfuse.available ? "Connected" : "Unavailable")}
          />
        </section>

        {error ? (
          <div className="mt-4 flex items-start gap-2 rounded border border-red-500/25 bg-red-500/[0.06] px-3 py-2 text-sm text-red-700 dark:text-red-300">
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : null}

        <section className="grid gap-8 border-b py-7 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="min-w-0 space-y-6">
            <div className="flex flex-wrap gap-4">
              <label className="min-w-[190px] flex-1 text-xs font-medium text-muted-foreground">
                {t("evaluations.suite", { defaultValue: "Suite" })}
                <select className="mt-2 h-9 w-full rounded border bg-background px-3 text-sm text-foreground" value={suite?.id ?? "office"} disabled>
                  <option value={suite?.id ?? "office"}>{suite?.label ?? "Office benchmark"}</option>
                </select>
              </label>
              <label className="min-w-[190px] flex-1 text-xs font-medium text-muted-foreground">
                {t("evaluations.profile", { defaultValue: "Profile" })}
                <select className="mt-2 h-9 w-full rounded border bg-background px-3 text-sm text-foreground" value={profile} onChange={(event) => setProfile(event.target.value)}>
                  {(suite?.profiles ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
              </label>
              <div className="min-w-[190px] flex-1 text-xs font-medium text-muted-foreground">
                {t("evaluations.action", { defaultValue: "Action" })}
                <div className="mt-2 grid h-9 grid-cols-2 rounded border p-0.5">
                  {(["run", "prepare"] as const).map((value) => (
                    <button key={value} type="button" disabled={profile === "ci" && value === "prepare"} onClick={() => setAction(value)} className={cn("rounded-sm text-xs capitalize disabled:opacity-35", action === value && "bg-foreground text-background")}>{value}</button>
                  ))}
                </div>
              </div>
            </div>

            <OptionGroup label={t("evaluations.benchmarks", { defaultValue: "Benchmarks" })} options={suite?.benchmarks ?? []} selected={benchmarks} onChange={setBenchmarks} />
            <OptionGroup label={t("evaluations.skills", { defaultValue: "Skills" })} options={suite?.skills ?? []} selected={skills} onChange={setSkills} />
            <div className="grid gap-6 lg:grid-cols-2">
              <OptionGroup label={t("evaluations.models", { defaultValue: "Model presets" })} options={suite?.model_presets ?? []} selected={modelPresets} onChange={setModelPresets} />
              <OptionGroup label={t("evaluations.runtimeProfiles", { defaultValue: "Runtime profiles" })} options={suite?.runtime_profiles ?? []} selected={runtimeProfiles} onChange={setRuntimeProfiles} />
            </div>
            {profile === "office-release" && benchmarks.length > 0 ? (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {benchmarks.map((benchmark) => {
                  const options = suite?.benchmark_samples?.[benchmark] ?? [];
                  const label = suite?.benchmarks.find((item) => item.id === benchmark)?.label ?? benchmark;
                  if (options.length === 0) return null;
                  return (
                    <label key={benchmark} className="block text-xs font-medium text-muted-foreground">
                      {label} sample
                      <select
                        className="mt-2 h-9 w-full rounded border bg-background px-3 text-sm text-foreground"
                        value={benchmarkSamples[benchmark] ?? options[options.length - 1] ?? ""}
                        onChange={(event) => setBenchmarkSamples((current) => ({
                          ...current,
                          [benchmark]: Number(event.target.value),
                        }))}
                      >
                        {options.map((value) => <option key={value} value={value}>{value}</option>)}
                      </select>
                    </label>
                  );
                })}
              </div>
            ) : null}
            {action === "prepare" && profile !== "ci" ? (
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={allowLicensedContent} onChange={(event) => setAllowLicensedContent(event.target.checked)} className="h-4 w-4 accent-foreground" />
                {t("evaluations.licensedPrepare", { defaultValue: "Prepare licensed content" })}
              </label>
            ) : null}
          </div>

          <aside className="border-l-0 xl:border-l xl:pl-8">
            <h2 className="text-sm font-semibold">{t("evaluations.estimate", { defaultValue: "Estimate and gates" })}</h2>
            <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
              <div><dt className="text-xs text-muted-foreground">Cases</dt><dd className="mt-1 font-medium">{formatNumber(readiness?.estimate.model_runs ?? readiness?.estimate.skill_runs)}</dd></div>
              <div><dt className="text-xs text-muted-foreground">Judge runs</dt><dd className="mt-1 font-medium">{formatNumber(readiness?.estimate.judge_runs)}</dd></div>
              <div className="col-span-2"><dt className="text-xs text-muted-foreground">Estimated tokens</dt><dd className="mt-1 text-2xl font-semibold">{formatNumber(totalTokens)}</dd></div>
            </dl>
            <div className="mt-5 space-y-2">
              {(readiness?.blockers ?? []).map((blocker) => (
                <div key={blocker} className="flex gap-2 text-xs text-red-700 dark:text-red-300"><Ban className="h-3.5 w-3.5 shrink-0" /><span>{blocker}</span></div>
              ))}
              {(readiness?.warnings ?? []).map((warning) => (
                <div key={warning} className="flex gap-2 text-xs text-amber-700 dark:text-amber-300"><AlertTriangle className="h-3.5 w-3.5 shrink-0" /><span>{warning}</span></div>
              ))}
              {readiness?.ready ? (
                <div className="flex gap-2 text-xs text-emerald-700 dark:text-emerald-300"><ShieldCheck className="h-3.5 w-3.5" /><span>Preflight passed</span></div>
              ) : null}
            </div>
            <Button className="mt-6 w-full" disabled={!canStart} onClick={requestStart}>
              {submitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
              {action === "prepare" ? t("evaluations.startPrepare", { defaultValue: "Start prepare" }) : t("evaluations.start", { defaultValue: "Start evaluation" })}
            </Button>
          </aside>
        </section>

        <section className="border-b py-7">
          <div className="mb-4 flex items-center justify-between"><h2 className="text-sm font-semibold">{t("evaluations.current", { defaultValue: "Current queue" })}</h2><span className="text-xs text-muted-foreground">{currentJobs.length} jobs</span></div>
          {currentJobs.length === 0 ? <p className="py-5 text-sm text-muted-foreground">{t("evaluations.noCurrent", { defaultValue: "No active or queued evaluations." })}</p> : (
            <div className="divide-y rounded border">
              {currentJobs.map((job) => <CurrentJobRow key={job.job_id} job={job} onCancel={() => client.cancelEvaluation(job.job_id)} />)}
            </div>
          )}
        </section>

        <section className="py-7">
          <div className="mb-4 flex items-center justify-between"><h2 className="text-sm font-semibold">{t("evaluations.history", { defaultValue: "Run history" })}</h2><span className="text-xs text-muted-foreground">Langfuse is the score source of truth</span></div>
          <div className="overflow-x-auto rounded border">
            <table className="w-full min-w-[1080px] text-left text-xs">
              <thead className="border-b bg-muted/35 text-muted-foreground"><tr><th className="w-10 px-3 py-2.5" /><th className="px-3 py-2.5 font-medium">Run</th><th className="px-3 py-2.5 font-medium">Variant</th><th className="px-3 py-2.5 font-medium">Status</th><th className="px-3 py-2.5 font-medium">Scores</th><th className="px-3 py-2.5 font-medium">Usage & performance</th><th className="px-3 py-2.5 font-medium">Review</th><th className="px-3 py-2.5 font-medium">Created</th><th className="px-3 py-2.5 text-right font-medium">Actions</th></tr></thead>
              <tbody className="divide-y">
                  {historicalJobs.map((job) => (
                  <HistoryRows key={`job-${job.job_id}`} id={job.job_id} label={`${job.suite_id} / ${job.profile}`} variant={job.request?.model_presets?.join(", ") ?? job.action ?? "-"} status={job.status} failure={job.failure} scores={job.aggregate_scores ?? {}} usage={job.usage} metrics={job.metrics} review={job.review_status ?? "-"} created={job.created_at} links={job.langfuse_links ?? []} progress={job.total_cases ? `${job.completed_cases ?? 0}/${job.total_cases}${job.remaining_cases != null ? ` · ${job.remaining_cases} remaining` : ""}${job.resumed_cases ? ` · ${job.resumed_cases} reused` : ""}${job.resume_count ? ` · resumed ${job.resume_count}x` : ""}` : undefined} expanded={expandedRun === job.job_id} cases={cases[job.job_id]} casesLoading={casesLoading === job.job_id} onToggle={() => void toggleCases(job.job_id)} onCaseRerun={(item) => client.rerunEvaluationCase(job.job_id, item.benchmark ?? "", item.skill ?? "", item.case_id, item.model_preset)} onResume={job.resumable && ["failed", "cancelled", "interrupted"].includes(job.status) ? () => client.resumeEvaluation(job.job_id) : undefined} onRetry={["failed", "cancelled", "interrupted"].includes(job.status) ? () => client.retryEvaluation(job.job_id) : undefined} onDelete={() => { setDeleteError(null); setDeleteTarget({ id: job.job_id, label: `${job.suite_id} / ${job.profile}`, remote: false }); }} />
                ))}
                {(runs?.langfuse.runs ?? []).map((run) => (
                  <HistoryRows key={`remote-${run.dataset_run_id}`} id={run.dataset_run_id} label={run.name} variant={[run.benchmark, run.skill, run.model_preset].filter(Boolean).join(" / ")} status={run.status} scores={run.aggregate_scores} usage={run.usage} metrics={run.metrics} review={run.review_status} created={run.created_at} links={run.langfuse_url ? [run.langfuse_url] : []} expanded={expandedRun === run.dataset_run_id} cases={cases[run.dataset_run_id]} casesLoading={casesLoading === run.dataset_run_id} onToggle={() => void toggleCases(run.dataset_run_id)} onDelete={() => { setDeleteError(null); setDeleteTarget({ id: run.dataset_run_id, label: run.name, remote: true }); }} />
                ))}
                {historicalJobs.length === 0 && (runs?.langfuse.runs.length ?? 0) === 0 ? <tr><td colSpan={9} className="px-3 py-10 text-center text-muted-foreground">{loading ? "Loading..." : t("evaluations.noHistory", { defaultValue: "No evaluation runs yet." })}</td></tr> : null}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("evaluations.confirmTitle", { defaultValue: "Confirm evaluation launch" })}</DialogTitle>
            <DialogDescription>
              {formatNumber(totalTokens)} estimated tokens · {formatNumber(readiness?.estimate.model_runs ?? readiness?.estimate.skill_runs)} case runs
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 rounded border bg-muted/25 p-3 text-sm">
            <p>Profile: <strong>{profile}</strong></p>
            <p>Data: <strong>{allowLicensedContent || action === "run" ? "licensed Dataset content may be uploaded" : "redacted prepare"}</strong></p>
            <p>Destination: <strong>Langfuse Japan Cloud</strong></p>
          </div>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5 h-4 w-4 accent-foreground" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            <span>{t("evaluations.confirmRisk", { defaultValue: "I reviewed the token estimate and data upload scope." })}</span>
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button disabled={!confirmed || submitting} onClick={start}>{submitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}Confirm and start</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open && !deleting) { setDeleteTarget(null); setDeleteError(null); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete evaluation history?</DialogTitle>
            <DialogDescription>
              {deleteTarget?.remote
                ? `Permanently delete ${deleteTarget.label} and its Langfuse Dataset Run data. This cannot be undone.`
                : `Delete ${deleteTarget?.label ?? "this evaluation"} from Mybot and permanently delete its linked Langfuse Dataset Run data. Local logs and private case checkpoints will also be removed.`}
            </DialogDescription>
          </DialogHeader>
          {deleteError ? (
            <div role="alert" className="flex items-start gap-2 rounded border border-red-500/25 bg-red-500/[0.06] px-3 py-2 text-sm text-red-700 dark:text-red-300">
              <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>删除失败：{deleteError}</span>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" disabled={deleting} onClick={() => { setDeleteTarget(null); setDeleteError(null); }}>Cancel</Button>
            <Button variant="destructive" disabled={deleting} onClick={() => void confirmDelete()}>
              {deleting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Trash2 className="mr-2 h-4 w-4" />}
              Delete history
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ReadinessItem({ label, ready, detail }: { label: string; ready: boolean | undefined; detail: string }) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      {ready === undefined ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : ready ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <XCircle className="h-4 w-4 text-red-600" />}
      <div className="min-w-0"><p className="text-xs font-medium">{label}</p><p className="truncate text-[11px] text-muted-foreground">{detail}</p></div>
    </div>
  );
}

function CurrentJobRow({ job, onCancel }: { job: EvaluationJob; onCancel: () => void }) {
  const total = job.total_cases ?? 0;
  const completed = job.completed_cases ?? 0;
  return (
    <div className="grid gap-3 px-4 py-3 md:grid-cols-[180px_minmax(0,1fr)_auto] md:items-center">
      <div><div className="flex items-center gap-2"><StatusPill status={job.status} /><span className="font-mono text-[10px] text-muted-foreground">{job.job_id.slice(0, 8)}</span></div><p className="mt-1 text-xs">{job.profile} · {job.action}{job.resume_count ? ` · resume ${job.resume_count}` : ""}</p></div>
      <div className="min-w-0"><div className="mb-1.5 flex justify-between gap-3 text-[11px] text-muted-foreground"><span className="truncate">{job.current_variant ?? job.phase}</span><span className="whitespace-nowrap">{completed}/{total}{job.resumed_cases ? ` · ${job.resumed_cases} reused` : ""}</span></div><ProgressBar completed={completed} total={total} /></div>
      <Button variant="ghost" size="sm" onClick={onCancel}><Ban className="mr-2 h-3.5 w-3.5" />Cancel</Button>
    </div>
  );
}

function HistoryRows({
  id, label, variant, status, failure, scores, usage, metrics, review, created, links, progress, expanded, cases, casesLoading, onToggle, onCaseRerun, onResume, onRetry, onDelete,
}: {
  id: string; label: string; variant: string; status: string; failure?: EvaluationFailure | null; scores: Record<string, unknown>; usage?: EvaluationUsage | null; metrics?: EvaluationMetrics | null; review: string; created?: string | null; links: string[]; progress?: string; expanded: boolean; cases?: EvaluationCase[]; casesLoading: boolean; onToggle: () => void; onCaseRerun?: (item: EvaluationCase) => void; onResume?: () => void; onRetry?: () => void; onDelete?: () => void;
}) {
  const uniqueLinks = [...new Set(links)];
  const scoreEntries = displayScoreEntries(scores);
  return (
    <>
      <tr className="hover:bg-muted/20">
        <td className="px-3 py-3"><Button variant="ghost" size="icon" className="h-6 w-6" onClick={onToggle} aria-label="Toggle case details">{expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}</Button></td>
        <td className="max-w-[260px] px-3 py-3"><p className="truncate font-medium" title={label}>{label}</p><p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{id.slice(0, 12)}{progress ? ` · ${progress}` : ""}</p></td>
        <td className="max-w-[210px] truncate px-3 py-3 text-muted-foreground" title={variant}>{variant || "-"}</td>
        <td className="px-3 py-3"><div className="flex items-center gap-1"><StatusPill status={status} /><FailureDetailsButton failure={failure} /></div></td>
        <td className="px-3 py-3">{scoreEntries.length ? scoreEntries.map(([name, value]) => <div key={name}><span className="text-muted-foreground">{name}</span> {formatScore(value)}</div>) : "-"}</td>
        <td className="px-3 py-3"><UsageSummary usage={usage} metrics={metrics} /></td>
        <td className="px-3 py-3 text-muted-foreground">{review}</td>
        <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{formatDate(created)}</td>
        <td className="px-3 py-3"><div className="flex justify-end gap-1"><LangfuseLinks links={uniqueLinks} />{onResume ? <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onResume} title="Resume unfinished cases" aria-label="Resume unfinished cases"><Play className="h-3.5 w-3.5" /></Button> : null}{onRetry ? <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onRetry} title="Retry from scratch" aria-label="Retry from scratch"><RotateCcw className="h-3.5 w-3.5" /></Button> : null}{onDelete ? <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive hover:text-destructive" onClick={onDelete} title="Delete evaluation history" aria-label="Delete evaluation history"><Trash2 className="h-3.5 w-3.5" /></Button> : null}</div></td>
      </tr>
      {expanded ? <tr><td colSpan={9} className="bg-muted/15 px-6 py-4">{casesLoading ? <div className="flex items-center gap-2 text-muted-foreground"><Loader2 className="h-3.5 w-3.5 animate-spin" />Loading cases...</div> : <CaseTable cases={cases ?? []} onRerun={onCaseRerun} />}</td></tr> : null}
    </>
  );
}

function LangfuseLinks({ links }: { links: string[] }) {
  if (links.length === 0) return null;
  if (links.length === 1) {
    return <Button asChild variant="ghost" size="icon" className="h-7 w-7"><a href={links[0]} target="_blank" rel="noreferrer" title="Open Langfuse" aria-label="Open Langfuse"><ExternalLink className="h-3.5 w-3.5" /></a></Button>;
  }
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="h-7 w-7" title={`Open ${links.length} Langfuse runs`} aria-label={`Open ${links.length} Langfuse runs`}><ExternalLink className="h-3.5 w-3.5" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {links.map((link, index) => (
          <DropdownMenuItem key={link} asChild>
            <a href={link} target="_blank" rel="noreferrer"><ExternalLink className="h-3.5 w-3.5" />Langfuse run {index + 1}</a>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function CaseTable({ cases, onRerun }: { cases: EvaluationCase[]; onRerun?: (item: EvaluationCase) => void }) {
  if (cases.length === 0) return <p className="text-muted-foreground">No case details available.</p>;
  const variantFields = caseVariantFields(cases);
  return (
    <div className="overflow-x-auto"><table className="w-full min-w-[920px]"><thead className="text-muted-foreground"><tr><th className="pb-2 font-medium">Case</th><th className="pb-2 font-medium">Variant</th><th className="pb-2 font-medium">Execution</th><th className="pb-2 font-medium">Scores</th><th className="pb-2 font-medium">Usage & performance</th><th className="pb-2 text-right font-medium">Trace</th><th className="pb-2 text-right font-medium">Action</th></tr></thead><tbody className="divide-y">{cases.map((item, index) => <tr key={`${item.case_id}-${item.model_preset ?? ""}-${index}`}><td className="max-w-[300px] truncate py-2 pr-3 font-mono text-[10px]" title={item.case_id}>{item.case_id}</td><td className="py-2 pr-3 text-muted-foreground">{caseVariantLabel(item, variantFields)}</td><td className="py-2 pr-3"><StatusPill status={item.status} /></td><td className="py-2 pr-3">{item.scores ? displayScoreEntries(item.scores).map(([name, value]) => <span key={name} className="mr-3"><span className="text-muted-foreground">{name}</span> {formatScore(value)}</span>) : item.score_status ?? "-"}</td><td className="py-2 pr-3"><UsageSummary usage={item.usage} metrics={item.metrics} /></td><td className="py-2 text-right">{item.trace_url || item.langfuse_url ? <Button asChild variant="ghost" size="icon" className="h-7 w-7"><a href={item.trace_url ?? item.langfuse_url ?? "#"} target="_blank" rel="noreferrer"><ExternalLink className="h-3.5 w-3.5" /></a></Button> : "-"}</td><td className="py-2 text-right">{onRerun && ["failed", "completed"].includes(item.status) ? <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => onRerun(item)} title="Rerun this case" aria-label={`Rerun case ${item.case_id}`}><RotateCcw className="h-3.5 w-3.5" /></Button> : "-"}</td></tr>)}</tbody></table></div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  RefreshCw,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { fetchTurnTrace } from "@/lib/api";
import { fmtDateTime, formatTurnLatency } from "@/lib/format";
import type { TraceSpanPayload, TurnTracePayload } from "@/lib/types";
import { cn } from "@/lib/utils";

interface TracePanelProps {
  sessionKey: string;
  token: string;
  turnId: string | null;
  live?: boolean;
}

export function TracePanel({ sessionKey, token, turnId, live = false }: TracePanelProps) {
  const { i18n, t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [trace, setTrace] = useState<TurnTracePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    if (!turnId) {
      setTrace(null);
      return;
    }
    setLoading(true);
    try {
      setTrace(await fetchTurnTrace(token, sessionKey, turnId));
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [sessionKey, token, turnId]);

  useEffect(() => {
    if (!open) return;
    void load();
    if (!live) return;
    const interval = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(interval);
  }, [live, load, open]);

  useEffect(() => {
    setTrace(null);
    setFailed(false);
  }, [sessionKey, turnId]);

  const durationMs = useMemo(() => {
    const durations = trace?.spans
      .map((span) => span.duration_ms)
      .filter((value): value is number => typeof value === "number") ?? [];
    return durations.length ? Math.max(...durations) : null;
  }, [trace]);
  const locale = i18n.resolvedLanguage || i18n.language;

  return (
    <>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={cn(
                "host-no-drag h-8 w-8 rounded-full text-muted-foreground/80",
                "hover:bg-accent/40 hover:text-foreground",
              )}
              aria-label={t("thread.trace.open")}
              onClick={() => setOpen(true)}
            >
              <Activity className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{t("thread.trace.open")}</TooltipContent>
        </Tooltip>
      </TooltipProvider>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          className="w-[min(96vw,34rem)] gap-0 p-0 sm:max-w-[34rem]"
        >
          <div className="border-b px-5 pb-4 pt-5">
            <div className="flex items-center gap-3 pr-8">
              <div className="min-w-0 flex-1">
                <SheetTitle className="text-base font-medium">
                  {t("thread.trace.title")}
                </SheetTitle>
                <SheetDescription className="mt-1 text-xs">
                  {trace?.source === "langfuse"
                    ? t("thread.trace.sourceLangfuse")
                    : t("thread.trace.sourceLocal")}
                </SheetDescription>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 rounded-md"
                aria-label={t("thread.trace.refresh")}
                onClick={() => void load()}
                disabled={!turnId || loading}
              >
                <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
              </Button>
              {trace?.trace_url ? (
                <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" asChild>
                  <a
                    href={trace.trace_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={t("thread.trace.openExternal")}
                  >
                    <ExternalLink className="h-4 w-4" />
                  </a>
                </Button>
              ) : null}
            </div>

            {trace?.available ? (
              <div className="mt-4 grid grid-cols-3 gap-3 border-t pt-3 text-xs">
                <TraceMetric label={t("thread.trace.spans")} value={String(trace.spans.length)} />
                <TraceMetric
                  label={t("thread.trace.tokens")}
                  value={trace.usage.total_tokens.toLocaleString(locale)}
                />
                <TraceMetric
                  label={t("thread.trace.duration")}
                  value={durationMs === null ? "-" : formatTurnLatency(durationMs, locale)}
                />
              </div>
            ) : null}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
            {!turnId ? (
              <TraceEmpty text={t("thread.trace.noTurn")} />
            ) : failed ? (
              <TraceEmpty text={t("thread.trace.loadFailed")} error />
            ) : loading && !trace ? (
              <TraceEmpty text={t("thread.trace.loading")} loading />
            ) : !trace?.available ? (
              <TraceEmpty text={t("thread.trace.empty")} />
            ) : (
              <div className="space-y-2">
                {trace.spans.map((span) => (
                  <TraceSpanRow key={span.span_id} span={span} locale={locale} />
                ))}
              </div>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}

function TraceMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="truncate text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-mono text-sm text-foreground">{value}</div>
    </div>
  );
}

function TraceEmpty({
  text,
  error = false,
  loading = false,
}: {
  text: string;
  error?: boolean;
  loading?: boolean;
}) {
  const Icon = error ? AlertCircle : loading ? RefreshCw : Activity;
  return (
    <div className="flex min-h-48 flex-col items-center justify-center gap-3 px-6 text-center text-sm text-muted-foreground">
      <Icon className={cn("h-5 w-5", error && "text-destructive", loading && "animate-spin")} />
      <span>{text}</span>
    </div>
  );
}

function TraceSpanRow({ span, locale }: { span: TraceSpanPayload; locale: string }) {
  const { t } = useTranslation();
  const StatusIcon = span.status === "error"
    ? AlertCircle
    : span.status === "completed"
      ? CheckCircle2
      : CircleDashed;
  const statusClass = span.status === "error"
    ? "text-destructive"
    : span.status === "completed"
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-amber-600 dark:text-amber-400";
  const timestamp = fmtDateTime(span.started_at, locale);

  return (
    <details className="group rounded-md border border-border/70 bg-background open:bg-muted/20">
      <summary className="flex cursor-pointer list-none items-start gap-3 px-3 py-3 marker:hidden">
        <StatusIcon className={cn("mt-0.5 h-4 w-4 shrink-0", statusClass)} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <span className="truncate text-sm font-medium text-foreground">{span.name}</span>
            {span.actor ? (
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                {span.actor}
              </span>
            ) : null}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            {timestamp ? <span>{timestamp}</span> : null}
            {typeof span.duration_ms === "number" ? (
              <span>{formatTurnLatency(span.duration_ms, locale)}</span>
            ) : null}
            <span>{t("thread.trace.eventCount", { count: span.events.length })}</span>
          </div>
        </div>
      </summary>
      <div className="border-t border-border/60 px-3 py-3">
        {span.events.length ? (
          <div className="space-y-3">
            {span.events.map((event, index) => (
              <div key={`${event.timestamp ?? "event"}-${index}`} className="border-l pl-3">
                <div className="text-xs font-medium text-foreground">{event.name}</div>
                {event.timestamp ? (
                  <div className="mt-0.5 text-[10px] text-muted-foreground">
                    {fmtDateTime(event.timestamp, locale)}
                  </div>
                ) : null}
                {Object.keys(event.attributes).length ? (
                  <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/60 p-2 font-mono text-[10px] leading-4 text-muted-foreground">
                    {JSON.stringify(event.attributes, null, 2)}
                  </pre>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <pre className="overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/60 p-2 font-mono text-[10px] leading-4 text-muted-foreground">
            {JSON.stringify(span.attributes, null, 2)}
          </pre>
        )}
      </div>
    </details>
  );
}

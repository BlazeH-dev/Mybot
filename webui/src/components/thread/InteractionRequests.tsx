import { useState } from "react";
import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { InteractionRequestPayload } from "@/lib/types";

function InteractionRequestCard({
  interaction,
  onRespond,
}: {
  interaction: InteractionRequestPayload;
  onRespond: (response: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const binding = interaction.payload?.binding as Record<string, unknown> | undefined;
  const rawReason = typeof binding?.reason === "string"
    ? binding.reason
    : t("thread.interaction.actionRequired", { defaultValue: "Action required" });
  const reasonKey = interaction.payload?.reason_i18n_key;
  const reason = typeof reasonKey === "string"
    ? t(reasonKey, { defaultValue: rawReason })
    : rawReason;
  const target = typeof binding?.target === "string" ? binding.target : undefined;
  const questions = interaction.questions?.length
    ? interaction.questions
    : [{
        id: "answer",
        header: t("thread.interaction.questionTitle", { defaultValue: "Your input is required" }),
        question: reason,
        options: null,
        multiple: false,
      }];
  const hasAnswers = questions.every((question) => {
    const value = answers[question.id];
    return Array.isArray(value) ? value.length > 0 : Boolean(value?.trim());
  });

  const setSingleAnswer = (questionId: string, value: string) => {
    setAnswers((current) => ({ ...current, [questionId]: value }));
  };

  const toggleMultipleAnswer = (questionId: string, value: string) => {
    setAnswers((current) => {
      const selected = Array.isArray(current[questionId]) ? current[questionId] : [];
      return {
        ...current,
        [questionId]: selected.includes(value)
          ? selected.filter((item) => item !== value)
          : [...selected, value],
      };
    });
  };

  const submitAnswers = () => {
    const response: Record<string, unknown> = { answers };
    if (questions.length === 1 && typeof answers[questions[0].id] === "string") {
      response.answer = answers[questions[0].id];
    }
    onRespond(response);
  };

  return (
    <div className="mb-2 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 shadow-sm">
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">
            {interaction.kind === "approval"
              ? t("thread.interaction.approvalTitle", { defaultValue: "Approval required" })
              : t("thread.interaction.questionTitle", { defaultValue: "Your input is required" })}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">{reason}</div>
          {target ? (
            <div className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
              {target}
            </div>
          ) : null}
          {interaction.kind === "approval" ? (
            <div className="mt-3 flex justify-end gap-2">
              <Button size="sm" onClick={() => onRespond({ approved: true })}>
                {t("thread.interaction.approveOnce", { defaultValue: "Approve once" })}
              </Button>
              <Button size="sm" variant="outline" onClick={() => onRespond({ approved: false })}>
                {t("thread.interaction.deny", { defaultValue: "Deny" })}
              </Button>
            </div>
          ) : (
            <div className="mt-3 space-y-3">
              {questions.map((question) => (
                <div key={question.id} className="space-y-2">
                  <div>
                    <div className="text-xs font-medium">
                      {question.header_i18n_key
                        ? t(question.header_i18n_key, { defaultValue: question.header })
                        : question.header}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {question.question_i18n_key
                        ? t(question.question_i18n_key, { defaultValue: question.question })
                        : question.question}
                    </div>
                  </div>
                  {question.options?.length ? (
                    <div className="flex flex-wrap gap-2">
                      {question.options.map((option) => {
                        const current = answers[question.id];
                        const selected = Array.isArray(current)
                          ? current.includes(option.label)
                          : current === option.label;
                        return (
                          <Button
                            key={option.label}
                            type="button"
                            size="sm"
                            variant={selected ? "default" : "outline"}
                            title={option.description_i18n_key
                              ? t(option.description_i18n_key, { defaultValue: option.description })
                              : option.description}
                            onClick={() => question.multiple
                              ? toggleMultipleAnswer(question.id, option.label)
                              : setSingleAnswer(question.id, option.label)}
                          >
                            {option.label_i18n_key
                              ? t(option.label_i18n_key, { defaultValue: option.label })
                              : option.label}
                          </Button>
                        );
                      })}
                    </div>
                  ) : (
                    <input
                      value={typeof answers[question.id] === "string" ? answers[question.id] : ""}
                      onChange={(event) => setSingleAnswer(question.id, event.target.value)}
                      className="w-full rounded-md border bg-background px-2 py-1 text-sm"
                      placeholder={t("thread.interaction.answerPlaceholder", { defaultValue: "Type your answer" })}
                    />
                  )}
                </div>
              ))}
              <Button
                size="sm"
                disabled={!hasAnswers}
                onClick={submitAnswers}
              >
                {t("thread.interaction.continue", { defaultValue: "Continue" })}
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function InteractionRequests({
  interactions,
  onRespond,
}: {
  interactions: InteractionRequestPayload[];
  onRespond: (
    requestId: string,
    expectedRevision: number,
    response: Record<string, unknown>,
  ) => void;
}) {
  const visibleInteractions = interactions.filter(
    (interaction) => !["plan_confirmation", "reflection_decision"].includes(interaction.kind),
  );
  if (!visibleInteractions.length) return null;
  return (
    <div data-testid="interaction-requests">
      {visibleInteractions.map((interaction) => (
        <InteractionRequestCard
          key={interaction.request_id}
          interaction={interaction}
          onRespond={(response) => onRespond(
            interaction.request_id,
            interaction.revision,
            response,
          )}
        />
      ))}
    </div>
  );
}

import { act, render } from '@testing-library/react';
import { vi } from 'vitest';

import { QuestionCard, type QuestionItem } from '@/question-card';
import type { AnswerFormat, FormPage, FormPrefill, JsonSchema } from '@/transcript-model';

export const CALLBACK = 'https://app.example/api/interactions/callback/ticket-1';

/** The clock the timer tests run on. Both the fake timers and `Date.now` are
 * pinned here, so a second on this card's clock is a second the test advanced —
 * never wall time the test box happened to spend elsewhere. */
export const T0 = Date.parse('2026-08-07T12:00:00Z');

export function freezeClock(): void {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
}

/** The default answer schema a `form` question renders with. */
export const FORM_SCHEMA: JsonSchema = {
  type: 'object',
  properties: { note: { type: 'string' } },
};

/** The format-dependent extras are the format's own, so they are not something an
 * override can change: only `external` reaches the page with a callback ticket, and
 * only `form` with an answer schema. */
export function question(
  format: AnswerFormat,
  overrides: Partial<Omit<QuestionItem, 'answerFormat' | 'callbackUrl' | 'schema'>> = {},
): QuestionItem {
  const base = {
    kind: 'question',
    id: 'q1',
    interactionId: 'int-1',
    question: 'Deploy to production?',
    options: format === 'select' ? ['Now', 'Tonight'] : null,
    media: null,
    timeoutAt: new Date(Date.now() + 10 * 60_000).toISOString(),
    ts: new Date().toISOString(),
    ...overrides,
  } as const;
  if (format === 'external')
    return {
      ...base,
      answerFormat: format,
      callbackUrl: CALLBACK,
      schema: null,
      formData: null,
      pages: null,
    };
  if (format === 'form')
    return {
      ...base,
      answerFormat: format,
      callbackUrl: null,
      schema: FORM_SCHEMA,
      formData: null,
      pages: null,
    };
  return {
    ...base,
    answerFormat: format,
    callbackUrl: null,
    schema: null,
    formData: null,
    pages: null,
  };
}

/** A `form` question carrying an explicit schema plus the per-send prefill/steps. */
export function formQuestion(
  schema: JsonSchema,
  formData: FormPrefill | null,
  pages: readonly FormPage[] | null,
): QuestionItem {
  return {
    kind: 'question',
    id: 'q1',
    interactionId: 'int-1',
    question: 'Deploy to production?',
    options: null,
    media: null,
    timeoutAt: new Date(Date.now() + 10 * 60_000).toISOString(),
    ts: new Date().toISOString(),
    answerFormat: 'form',
    callbackUrl: null,
    schema,
    formData,
    pages,
  };
}

/** An `external` question pointing at `url` — the one format whose widget opens
 * the ticket it was given. */
export function externalQuestion(url: string): QuestionItem {
  return {
    ...question('external'),
    answerFormat: 'external',
    callbackUrl: url,
    schema: null,
    formData: null,
    pages: null,
  };
}

/** One turn of the card's self-rescheduling clock. React commits the tick's state
 * update when the act scope closes, so the NEXT timer is only armed by then —
 * one scope per tick. */
export async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** `seconds` turns of the per-second clock — one act scope each, so every tick
 * arms the next. */
export async function tickSeconds(seconds: number): Promise<void> {
  for (let second = 0; second < seconds; second += 1) await tick(1_000);
}

export function renderCard(item: QuestionItem, overrides = {}) {
  const onAnswer = vi.fn().mockResolvedValue(undefined);
  const onAnswered = vi.fn();
  render(
    <QuestionCard
      question={item}
      answered={false}
      onAnswer={onAnswer}
      onAnswered={onAnswered}
      locked={false}
      {...overrides}
    />,
  );
  return { onAnswer, onAnswered };
}

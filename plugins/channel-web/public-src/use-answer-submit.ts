/**
 * The question-answer and form-submit doors, each guarded against a stale session.
 * The conversation an answer/submission belongs to is read before it leaves; a "new
 * conversation" started while it was in flight drops the session it was sent on, so
 * its `session_missing` describes the conversation the visitor just left and must
 * not end the fresh one the rotate just minted.
 */
import type { RefObject } from 'react';
import { useCallback } from 'react';

import { answerQuestion, isSessionMissing, submitForm } from '@/api';

export interface AnswerSubmit {
  readonly onAnswer: (interactionId: string, answer: unknown) => Promise<void>;
  readonly onSubmitForm: (token: string, values: Record<string, unknown>) => Promise<void>;
}

export function useAnswerSubmit(params: {
  generationRef: RefObject<number>;
  onSessionEnded: () => void;
}): AnswerSubmit {
  const { generationRef, onSessionEnded } = params;

  const onAnswer = useCallback(
    async (interactionId: string, answer: unknown): Promise<void> => {
      const generation = generationRef.current;
      try {
        await answerQuestion(interactionId, answer);
      } catch (err) {
        if (generationRef.current === generation && isSessionMissing(err)) onSessionEnded();
        throw err;
      }
    },
    [generationRef, onSessionEnded],
  );

  const onSubmitForm = useCallback(
    async (token: string, values: Record<string, unknown>): Promise<void> => {
      const generation = generationRef.current;
      try {
        await submitForm(token, values);
      } catch (err) {
        if (generationRef.current === generation && isSessionMissing(err)) onSessionEnded();
        throw err;
      }
    },
    [generationRef, onSessionEnded],
  );

  return { onAnswer, onSubmitForm };
}

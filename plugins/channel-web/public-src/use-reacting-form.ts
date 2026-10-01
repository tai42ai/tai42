/**
 * The stateful core of the form renderer: the value/errors/options/slots/step state, and
 * the change / page-advance / submit handlers. A REACTING form (one that declares
 * `reactions` and carries an `interactionId`) rounds-trips each declared event to this
 * plugin's own reaction door and applies the returned update; only the latest response is
 * applied, and a failed round-trip surfaces LOUDLY and holds the form open, never a stale
 * value. A static form behaves exactly as before. The presentational `FormSurface` reads
 * what this returns; it owns no state of its own.
 */
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import { validateAgainstSchema } from '@tai42/studio-sdk';
import type { RefObject } from 'react';
import { useRef, useState } from 'react';

import { reactToForm } from '@/api';
import {
  changedTopLevelKeys,
  errorsForFields,
  firstPageWithError,
  initialFormValue,
  isPlainObject,
  resolvePages,
  useStepFocus,
} from '@/schema-form-helpers';
import type {
  FormOptionData,
  FormPage,
  FormPrefill,
  FormReactions,
  FormUpdate,
  ReactionEvent,
} from '@/transcript-model';

export interface ReactingFormInput {
  readonly schema: JsonSchema;
  readonly formData: FormPrefill | null;
  readonly pages: readonly FormPage[] | null;
  /** The form's reaction triggers, or `null` for a static form (an ask-less card). */
  readonly reactions: FormReactions | null;
  /** The interaction id the reaction door is addressed by, or `null` when the form does
   * not react. A reaction fires only when both this and `reactions` are set. */
  readonly interactionId: string | null;
  readonly sending: boolean;
  readonly onSubmit: (answer: unknown) => void;
}

export interface ReactingForm {
  readonly value: unknown;
  readonly errors: SchemaFormErrors;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly slots: Readonly<Record<string, unknown>>;
  readonly page: FormPage;
  readonly pageIndex: number;
  readonly pageCount: number;
  readonly stepped: boolean;
  readonly isLast: boolean;
  readonly reactionError: string | null;
  readonly busy: boolean;
  readonly pageRef: RefObject<HTMLDivElement | null>;
  readonly headingRef: RefObject<HTMLParagraphElement | null>;
  readonly onChange: (value: unknown) => void;
  readonly onBack: () => void;
  readonly onNext: () => void;
  readonly onSubmit: () => void;
}

type OptionMap = Readonly<Record<string, readonly FormOptionData[]>>;

interface FormState {
  readonly value: unknown;
  readonly replaceValue: (value: unknown) => void;
  readonly errors: SchemaFormErrors;
  readonly setErrors: (errors: SchemaFormErrors) => void;
  readonly reactionErrors: SchemaFormErrors;
  /** Clear BOTH the validation and reaction error bags — a step change starts clean. */
  readonly resetErrors: () => void;
  readonly options: OptionMap;
  readonly slots: Readonly<Record<string, unknown>>;
  readonly applyUpdate: (update: FormUpdate) => void;
}

/** The step a form falls back to only to satisfy the index type; it never renders. */
const EMPTY_PAGE: FormPage = { title: '', fields: [], display: [], kind: 'input' };

/** The value, validation/reaction errors, per-send choices, and display slots of one open
 * form, plus `applyUpdate` which folds a reaction's returned update into all of them. */
function useFormState(schema: JsonSchema, formData: FormPrefill | null): FormState {
  const [value, setValue] = useState<unknown>(() => initialFormValue(schema, formData));
  const [errors, setErrors] = useState<SchemaFormErrors>({});
  // Reaction messages are kept apart from the client validation bag so a reaction that
  // returns none clears its own without wiping a live validation error.
  const [reactionErrors, setReactionErrors] = useState<SchemaFormErrors>({});
  const [options, setOptions] = useState<OptionMap>(formData?.options ?? {});
  const [slots, setSlots] = useState<Record<string, unknown>>(() => ({
    ...(formData?.values ?? {}),
  }));
  const applyUpdate = (update: FormUpdate): void => {
    if (Object.keys(update.values).length > 0) {
      setValue((current: unknown) => ({
        ...(isPlainObject(current) ? current : {}),
        ...update.values,
      }));
    }
    if (Object.keys(update.options).length > 0)
      setOptions((cur) => ({ ...cur, ...update.options }));
    setReactionErrors(update.errors);
    if (Object.keys(update.display).length > 0) setSlots((cur) => ({ ...cur, ...update.display }));
  };
  return {
    value,
    replaceValue: setValue,
    errors,
    setErrors,
    reactionErrors,
    resetErrors: () => {
      setErrors({});
      setReactionErrors({});
    },
    options,
    slots,
    applyUpdate,
  };
}

interface ReactionRunner {
  readonly reacting: boolean;
  readonly reactionError: string | null;
  readonly clearError: () => void;
  readonly fireReaction: (
    event: ReactionEvent,
    partial: Record<string, unknown>,
  ) => Promise<FormUpdate | null>;
}

/** The reaction round-trip: fire one event, apply only the LATEST response, and surface a
 * failure loudly (returning `null` so a gated page/submit holds). */
function useReactionRunner(
  interactionId: string | null,
  applyUpdate: (update: FormUpdate) => void,
): ReactionRunner {
  const [reacting, setReacting] = useState(false);
  const [reactionError, setReactionError] = useState<string | null>(null);
  const seq = useRef(0);
  const fireReaction = async (
    event: ReactionEvent,
    partial: Record<string, unknown>,
  ): Promise<FormUpdate | null> => {
    if (interactionId === null) return null;
    const mine = ++seq.current;
    setReacting(true);
    setReactionError(null);
    try {
      const update = await reactToForm(interactionId, event, partial);
      if (mine !== seq.current) return update;
      applyUpdate(update);
      return update;
    } catch (error) {
      if (mine === seq.current)
        setReactionError(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      if (mine === seq.current) setReacting(false);
    }
  };
  return { reacting, reactionError, clearError: () => setReactionError(null), fireReaction };
}

interface HandlerContext {
  readonly schema: JsonSchema;
  readonly reactions: FormReactions | null;
  readonly page: FormPage;
  readonly stepped: boolean;
  readonly resolvedPages: readonly FormPage[];
  readonly st: FormState;
  readonly runner: ReactionRunner;
  readonly setPageIndex: (value: number | ((index: number) => number)) => void;
  readonly armStepMove: () => void;
  readonly onSubmit: (answer: unknown) => void;
}

/** The change / advance / submit / back handlers for one open form. Each gating path (a
 * declared field-change / page-advance / submit reaction) runs the reaction first and acts
 * only once it accepts; a failed or refusing reaction holds the form where it is. */
function makeHandlers(
  ctx: HandlerContext,
): Pick<ReactingForm, 'onChange' | 'onBack' | 'onNext' | 'onSubmit'> {
  const {
    schema,
    reactions,
    page,
    stepped,
    resolvedPages,
    st,
    runner,
    setPageIndex,
    armStepMove,
    onSubmit,
  } = ctx;
  const gateThen = (event: ReactionEvent, proceed: () => void): void => {
    void (async () => {
      const update = await runner.fireReaction(event, isPlainObject(st.value) ? st.value : {});
      if (update === null || Object.keys(update.errors).length > 0) return;
      proceed();
    })();
  };
  const advance = (): void => {
    st.resetErrors();
    armStepMove();
    setPageIndex((index) => index + 1);
  };
  const onChange = (next: unknown): void => {
    const changed = changedTopLevelKeys(st.value, next);
    st.replaceValue(next);
    if (reactions === null) return;
    const triggered = changed.find((key) => reactions.fieldChanged.includes(key));
    if (triggered !== undefined) {
      void runner.fireReaction(
        { kind: 'field_changed', field: triggered },
        isPlainObject(next) ? next : {},
      );
    }
  };
  const onNext = (): void => {
    const found = errorsForFields(validateAgainstSchema(schema, st.value), page.fields);
    st.setErrors(found);
    if (Object.keys(found).length > 0) return;
    if (reactions?.pageAdvanced.includes(page.title)) {
      gateThen({ kind: 'page_advanced', page: page.title }, advance);
      return;
    }
    advance();
  };
  const onSubmitForm = (): void => {
    const found = validateAgainstSchema(schema, st.value);
    st.setErrors(found);
    if (Object.keys(found).length > 0) {
      const target = stepped ? firstPageWithError(resolvedPages, found) : -1;
      if (target !== -1) setPageIndex(target);
      return;
    }
    if (reactions?.submitted) {
      gateThen({ kind: 'submitted' }, () => onSubmit(st.value));
      return;
    }
    onSubmit(st.value);
  };
  const onBack = (): void => {
    st.resetErrors();
    runner.clearError();
    armStepMove();
    setPageIndex((index) => Math.max(index - 1, 0));
  };
  return { onChange, onBack, onNext, onSubmit: onSubmitForm };
}

export function useReactingForm(input: ReactingFormInput): ReactingForm {
  const { schema, formData, pages, reactions, interactionId, sending, onSubmit } = input;
  const resolvedPages = resolvePages(schema, pages);
  const stepped = resolvedPages.length > 1;
  const [pageIndex, setPageIndex] = useState(0);
  const st = useFormState(schema, formData);
  const runner = useReactionRunner(interactionId, st.applyUpdate);
  const { pageRef, headingRef, armStepMove } = useStepFocus(pageIndex);
  // `resolvePages` always yields at least one page; the fallback never renders.
  const page = resolvedPages[Math.min(pageIndex, resolvedPages.length - 1)] ?? EMPTY_PAGE;
  const handlers = makeHandlers({
    schema,
    reactions,
    page,
    stepped,
    resolvedPages,
    st,
    runner,
    setPageIndex,
    armStepMove,
    onSubmit,
  });
  return {
    value: st.value,
    errors: { ...st.errors, ...st.reactionErrors },
    options: st.options,
    slots: st.slots,
    page,
    pageIndex,
    pageCount: resolvedPages.length,
    stepped,
    isLast: pageIndex >= resolvedPages.length - 1,
    reactionError: runner.reactionError,
    busy: sending || runner.reacting,
    pageRef,
    headingRef,
    ...handlers,
  };
}

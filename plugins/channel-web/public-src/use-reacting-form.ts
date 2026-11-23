/**
 * The stateful core of the form renderer: the value/errors/options/slots/step state, and
 * the change / page-advance / submit handlers. A REACTING form (one that declares
 * `reactions` and carries an `interactionId`) rounds-trips each declared event to this
 * plugin's own reaction door and applies the returned update; only the latest response is
 * applied, and a failed round-trip surfaces LOUDLY and holds the form open, never a stale
 * value. A static form fires no reaction and submits its value as entered. A submitted
 * reaction's update is part of the answer: it is folded into the value as every other
 * reaction's is (choices its lists no longer offer dropped, its values laid over) and the
 * result is validated again before it is posted; a page-advance reaction's folded page is
 * checked again the same way before the step turns. The presentational `FormSurface` reads
 * what this returns; it owns no state of its own.
 */
import type { JsonSchema, SchemaFormErrors } from '@tai42/studio-sdk';
import { changedTopLevelKeys, firstPageWithError, withFoldedUpdate } from '@tai42/studio-sdk';
import type { RefObject } from 'react';
import { useRef, useState } from 'react';

import { ChatApiError, reactToForm } from '@/api';
import { isRecord } from '@/guards';
import type { SdkFieldWording } from '@/schema-form-helpers';
import {
  answerErrors,
  errorsForFields,
  imageSlotsOf,
  initialFormValue,
  resolvePages,
  toSdkFieldWording,
  useStepFocus,
} from '@/schema-form-helpers';
import type {
  DisplayText,
  FieldWording,
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
  /** The caller's per-field words, keyed by property name: a field's picker placeholder
   * (`choiceLabel`) and the sentence (`invalid`) that replaces the validator's word for an
   * answer outside that field's shape. */
  readonly fieldWording: Readonly<Record<string, FieldWording>>;
  readonly onSubmit: (answer: unknown) => void;
}

export interface ReactingForm {
  readonly value: unknown;
  readonly errors: SchemaFormErrors;
  readonly options: Readonly<Record<string, readonly FormOptionData[]>>;
  readonly slots: Readonly<Record<string, DisplayText>>;
  readonly imageSources: ImageSources;
  readonly page: FormPage;
  readonly pageIndex: number;
  readonly pageCount: number;
  readonly stepped: boolean;
  readonly isLast: boolean;
  readonly reactionError: ReactionError | null;
  readonly busy: boolean;
  readonly pageRef: RefObject<HTMLDivElement | null>;
  readonly headingRef: RefObject<HTMLParagraphElement | null>;
  readonly onChange: (value: unknown) => void;
  readonly onBack: () => void;
  readonly onNext: () => void;
  readonly onSubmit: () => void;
}

type OptionMap = Readonly<Record<string, readonly FormOptionData[]>>;
type ImageSources = ReadonlyMap<string, string | null>;

interface FormState {
  readonly value: unknown;
  readonly replaceValue: (value: unknown) => void;
  readonly errors: SchemaFormErrors;
  readonly setErrors: (errors: SchemaFormErrors) => void;
  readonly reactionErrors: SchemaFormErrors;
  /** Clear BOTH the validation and reaction error bags — a step change starts clean. */
  readonly resetErrors: () => void;
  readonly options: OptionMap;
  readonly slots: Readonly<Record<string, DisplayText>>;
  readonly imageSources: ImageSources;
  readonly applyUpdate: (update: FormUpdate) => void;
}

/** The step a form falls back to only to satisfy the index type; it never renders. */
const EMPTY_PAGE: FormPage = {
  title: '',
  fields: [],
  display: [],
  kind: 'input',
  buttonLabel: null,
};

/** The value, validation/reaction errors, per-send choices, display slots and image-slot
 * sources of one open form, plus `applyUpdate` which folds a reaction's returned update into
 * all of them. Display slots and image sources come from a reaction only, never from the send
 * values, so every value a slot draws went through the update parser's vetting. */
function useFormState(schema: JsonSchema, formData: FormPrefill | null): FormState {
  const [value, setValue] = useState<unknown>(() => initialFormValue(schema, formData));
  const [errors, setErrors] = useState<SchemaFormErrors>({});
  // Reaction messages are kept apart from the client validation bag so a reaction that
  // returns none clears its own without wiping a live validation error.
  const [reactionErrors, setReactionErrors] = useState<SchemaFormErrors>({});
  const [options, setOptions] = useState<OptionMap>(formData?.options ?? {});
  const [slots, setSlots] = useState<Record<string, DisplayText>>({});
  const [imageSources, setImageSources] = useState<ImageSources>(() => new Map());
  const applyUpdate = (update: FormUpdate): void => {
    if (Object.keys(update.options).length > 0 || Object.keys(update.values).length > 0) {
      setValue((current: unknown) => withFoldedUpdate(schema, current, update));
    }
    if (Object.keys(update.options).length > 0)
      setOptions((cur) => ({ ...cur, ...update.options }));
    setReactionErrors(update.errors);
    if (Object.keys(update.display).length > 0) setSlots((cur) => ({ ...cur, ...update.display }));
    if (update.images.size > 0) setImageSources((cur) => new Map([...cur, ...update.images]));
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
    imageSources,
    applyUpdate,
  };
}

/** A failed reaction: the visitor-facing message, and the react door's own reason for
 * refusing when it gave one (`null` otherwise). */
export interface ReactionError {
  readonly message: string;
  readonly reason: string | null;
}

interface ReactionRunner {
  readonly reacting: boolean;
  readonly reactionError: ReactionError | null;
  readonly clearError: () => void;
  readonly fireReaction: (
    event: ReactionEvent,
    partial: Record<string, unknown>,
  ) => Promise<FormUpdate | null>;
}

/** The reaction round-trip: fire one event, apply only the LATEST response, and surface a
 * failure loudly (returning `null` so a gated page/submit holds). `pages` are the form's
 * resolved pages, whose image slots the update parser vets. */
function useReactionRunner(
  interactionId: string | null,
  pages: readonly FormPage[],
  applyUpdate: (update: FormUpdate) => void,
): ReactionRunner {
  const [reacting, setReacting] = useState(false);
  const [reactionError, setReactionError] = useState<ReactionError | null>(null);
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
      const update = await reactToForm(interactionId, event, partial, imageSlotsOf(pages));
      if (mine !== seq.current) return update;
      applyUpdate(update);
      return update;
    } catch (error) {
      if (mine === seq.current)
        setReactionError({
          message: error instanceof Error ? error.message : String(error),
          reason: error instanceof ChatApiError ? error.reason : null,
        });
      return null;
    } finally {
      if (mine === seq.current) setReacting(false);
    }
  };
  return { reacting, reactionError, clearError: () => setReactionError(null), fireReaction };
}

interface HandlerContext {
  /** The published schema; each validation stamps the current choice lists onto it. */
  readonly schema: JsonSchema;
  readonly fieldWording: SdkFieldWording;
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

interface AnswerChecks {
  /** Check the shown page's fields of `answer` with `options` as their choices; show what
   * fails and report whether the page passes. */
  readonly pagePasses: (answer: unknown, options: OptionMap) => boolean;
  /** Check the whole answer with `options` as its fields' choices; show what fails (turning a
   * stepped form to the first page carrying one) and report whether it passes. */
  readonly wholePasses: (answer: unknown, options: OptionMap) => boolean;
}

/** The checks an answer meets before the form moves on, each showing the errors it finds. */
function makeAnswerChecks(ctx: HandlerContext): AnswerChecks {
  const { schema, fieldWording, page, stepped, resolvedPages, st, setPageIndex } = ctx;
  const pagePasses = (answer: unknown, options: OptionMap): boolean => {
    const found = errorsForFields(answerErrors(schema, options, answer, fieldWording), page.fields);
    st.setErrors(found);
    return Object.keys(found).length === 0;
  };
  const wholePasses = (answer: unknown, options: OptionMap): boolean => {
    const found = answerErrors(schema, options, answer, fieldWording);
    st.setErrors(found);
    if (Object.keys(found).length === 0) return true;
    const target = stepped ? firstPageWithError(resolvedPages, found) : -1;
    if (target !== -1) setPageIndex(target);
    return false;
  };
  return { pagePasses, wholePasses };
}

/** The change / advance / submit / back handlers for one open form. Each gating path (a
 * declared field-change / page-advance / submit reaction) runs the reaction first and acts
 * only once it accepts; a failed or refusing reaction holds the form where it is. */
function makeHandlers(
  ctx: HandlerContext,
): Pick<ReactingForm, 'onChange' | 'onBack' | 'onNext' | 'onSubmit'> {
  const { schema, reactions, page, st, runner, setPageIndex, armStepMove, onSubmit } = ctx;
  const { pagePasses, wholePasses } = makeAnswerChecks(ctx);
  const gateThen = (event: ReactionEvent, proceed: (update: FormUpdate) => void): void => {
    void (async () => {
      const update = await runner.fireReaction(event, isRecord(st.value) ? st.value : {});
      if (update === null || Object.keys(update.errors).length > 0) return;
      proceed(update);
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
        isRecord(next) ? next : {},
      );
    }
  };
  const onNext = (): void => {
    if (!pagePasses(st.value, st.options)) return;
    if (reactions?.pageAdvanced.includes(page.title)) {
      // The page the reaction's folded update leaves is checked again before the step turns:
      // a reaction that withdraws a required pick holds the step on its sentence.
      gateThen({ kind: 'page_advanced', page: page.title }, (update) => {
        const answer = withFoldedUpdate(schema, st.value, update);
        if (pagePasses(answer, { ...st.options, ...update.options })) advance();
      });
      return;
    }
    advance();
  };
  const onSubmitForm = (): void => {
    if (!wholePasses(st.value, st.options)) return;
    if (reactions?.submitted) {
      // The answer is the reaction's update folded into the vetted one, as the form state
      // folds it, and checked again against the choices it returned: a reaction that clears
      // a required field holds the form open.
      gateThen({ kind: 'submitted' }, (update) => {
        const answer = withFoldedUpdate(schema, st.value, update);
        if (wholePasses(answer, { ...st.options, ...update.options })) onSubmit(answer);
      });
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
  const { schema, formData, pages, reactions, interactionId, sending, fieldWording, onSubmit } =
    input;
  const resolvedPages = resolvePages(schema, pages);
  const stepped = resolvedPages.length > 1;
  const [pageIndex, setPageIndex] = useState(0);
  const st = useFormState(schema, formData);
  const runner = useReactionRunner(interactionId, resolvedPages, st.applyUpdate);
  const { pageRef, headingRef, armStepMove } = useStepFocus(pageIndex);
  // `resolvePages` always yields at least one page; the fallback never renders.
  const page = resolvedPages[Math.min(pageIndex, resolvedPages.length - 1)] ?? EMPTY_PAGE;
  const handlers = makeHandlers({
    schema,
    fieldWording: toSdkFieldWording(fieldWording),
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
    imageSources: st.imageSources,
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

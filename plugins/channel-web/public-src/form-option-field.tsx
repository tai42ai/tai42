/**
 * The control for a form field the sender supplied a per-send choice list for: a
 * checkbox group for an array-shaped property (its value the checked choices, in the
 * list's order) and a native select otherwise (its value the one picked choice).
 */
import type { JsonSchema } from '@tai42/studio-sdk';
import { Checkbox, isArraySchema } from '@tai42/studio-sdk';
import type { ReactElement } from 'react';

import { isRecord } from '@/guards';
import type { FormOptionData } from '@/transcript-model';

/** The text shown for one option: its label (falling back to the value) and, when it
 * carries a second line, that line folded on after an em dash — a native `<option>` and a
 * checkbox label have no slot for a second line, so the content rides the option text
 * rather than being lost. */
function optionText(option: FormOptionData): string {
  const label = option.label ?? option.value;
  const description =
    typeof option.description === 'string' && option.description.trim() !== ''
      ? option.description
      : null;
  return description === null ? label : `${label} — ${description}`;
}

/** A per-send option field: a native `<select>` whose options show their label
 * (falling back to the value) and post their value. Native so it is keyboard
 * reachable and themed by the widget's tokens; a leading placeholder keeps it
 * controlled and unselected until the visitor (or a prefill) picks a value. A field
 * `hint` (the schema property's second line) draws under the label, matching the SDK's
 * own field hint; an option's second line folds onto its option text. */
function OptionSelect({
  id,
  label,
  placeholder,
  hint,
  options,
  value,
  error,
  onChange,
}: {
  readonly id: string;
  readonly label: string;
  readonly placeholder: string;
  readonly hint: string | undefined;
  readonly options: readonly FormOptionData[];
  readonly value: unknown;
  readonly error: string | undefined;
  readonly onChange: (value: string) => void;
}): ReactElement {
  const selected = typeof value === 'string' ? value : '';
  return (
    <div className="tcw-form-field">
      <label className="tcw-form-field-label" htmlFor={id}>
        {label}
      </label>
      {hint !== undefined ? <p className="tai-field-hint">{hint}</p> : null}
      <select
        id={id}
        className="tcw-select"
        value={selected}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="" disabled>
          {placeholder}
        </option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {optionText(option)}
          </option>
        ))}
      </select>
      {error !== undefined ? (
        <span className="tcw-form-field-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

/** A per-send option field over an array: a checkbox group, one box per choice, named
 * by its legend. Its value is the checked choices' values in the choices' order (never the
 * click order); an entry the choices do not offer shows no box. The field `hint` draws
 * under the legend, as on the select. */
function OptionChecklist({
  label,
  hint,
  options,
  value,
  error,
  onChange,
}: {
  readonly label: string;
  readonly hint: string | undefined;
  readonly options: readonly FormOptionData[];
  readonly value: unknown;
  readonly error: string | undefined;
  readonly onChange: (value: string[]) => void;
}): ReactElement {
  const checked: readonly unknown[] = Array.isArray(value) ? value : [];
  const toggle = (choice: string, on: boolean): void => {
    onChange(
      options
        .map((option) => option.value)
        .filter((candidate) => (candidate === choice ? on : checked.includes(candidate))),
    );
  };
  return (
    <fieldset className="tcw-form-field">
      <legend className="tcw-form-field-label">{label}</legend>
      {hint !== undefined ? <p className="tai-field-hint">{hint}</p> : null}
      {options.map((option) => (
        <Checkbox
          key={option.value}
          label={optionText(option)}
          checked={checked.includes(option.value)}
          onCheckedChange={(on) => toggle(option.value, on)}
        />
      ))}
      {error !== undefined ? (
        <span className="tcw-form-field-error" role="alert">
          {error}
        </span>
      ) : null}
    </fieldset>
  );
}

/** A field with a per-send choice list: its label (the property's title, else its name)
 * and hint, drawn as a checkbox group for an array-shaped property and as a select
 * otherwise; the chosen value is set on the form value under the field's name. */
export function OptionField({
  field,
  prop,
  choices,
  value,
  error,
  id,
  placeholder,
  onChange,
}: {
  readonly field: string;
  readonly prop: JsonSchema | undefined;
  readonly choices: readonly FormOptionData[];
  readonly value: unknown;
  readonly error: string | undefined;
  readonly id: string;
  readonly placeholder: string;
  readonly onChange: (value: unknown) => void;
}): ReactElement {
  const label = typeof prop?.title === 'string' ? prop.title : field;
  const hint =
    typeof prop?.description === 'string' && prop.description.trim() !== ''
      ? prop.description
      : undefined;
  const fieldValue = isRecord(value) ? value[field] : undefined;
  const setField = (next: unknown): void =>
    onChange(isRecord(value) ? { ...value, [field]: next } : { [field]: next });
  if (prop !== undefined && isArraySchema(prop)) {
    return (
      <OptionChecklist
        label={label}
        hint={hint}
        options={choices}
        value={fieldValue}
        error={error}
        onChange={setField}
      />
    );
  }
  return (
    <OptionSelect
      id={id}
      label={label}
      placeholder={placeholder}
      hint={hint}
      options={choices}
      value={fieldValue}
      error={error}
      onChange={setField}
    />
  );
}

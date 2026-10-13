/**
 * The mint-entry-code flow: a form (optional label + optional future expiry) that,
 * on success, becomes a SHOWN-ONCE reveal of the raw code and the chat URL that
 * carries it.
 *
 * The code is SHOWN ONCE: the mint reply's raw `code` lives only in this dialog's
 * local state, rendered in a `CopyField` beside the composed chat URL under a loud
 * "shown once" caption. There is no re-reveal affordance — the server cannot
 * reproduce a code; a new one means revoke + mint. The raw value is never logged.
 * Every failure (an invalid/past expiry, a 403, or any other status) surfaces
 * LOUDLY inline — never swallowed.
 */
import {
  Button,
  CopyField,
  Dialog,
  errorMessage,
  ErrorState,
  Field,
  Spinner,
  TextInput,
  useApi,
} from '@tai42/studio-sdk';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { type CSSProperties, type ReactNode, type SyntheticEvent, useState } from 'react';

import { composeChatUrl } from './compose-chat-url';
import { formatInstant } from './format';
import { type MintBody, mintCode } from './gate-client';
import type { WebEntryCodeMinted } from './gate-schemas';
import { entryGateKey } from './keys';

const captionStyle: CSSProperties = {
  margin: 0,
  fontSize: 'var(--tai-text-sm)',
  color: 'var(--tai-color-text-muted)',
};

const sectionStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--tai-space-4)',
};

export function MintEntryCodeDialog({
  base,
  identity,
  onClose,
}: {
  /** The web channel item's mount base the gate doors and the chat page live under. */
  readonly base: string;
  readonly identity: string;
  readonly onClose: () => void;
}): ReactNode {
  const api = useApi();
  const queryClient = useQueryClient();

  const [label, setLabel] = useState('');
  const [expiresLocal, setExpiresLocal] = useState('');
  const [expiryError, setExpiryError] = useState<string | null>(null);

  // The minted code is held in local state (shown once): the raw code rides this
  // reply exactly once and never leaves the rendered CopyField.
  const [minted, setMinted] = useState<WebEntryCodeMinted | null>(null);

  const mutation = useMutation({
    mutationFn: (body: MintBody) => mintCode(api.request, base, identity, body),
    onSuccess: (created) => {
      setMinted(created);
      void queryClient.invalidateQueries({ queryKey: entryGateKey(identity) });
    },
  });

  const chatUrl = minted !== null ? composeChatUrl(api.baseUrl, base, identity, minted.code) : null;

  const onSubmit = (event: SyntheticEvent): void => {
    event.preventDefault();
    setExpiryError(null);

    // A `datetime-local` control yields either '' or a valid local instant, so a
    // non-empty value always parses; only its being in the future is checked here.
    let expiresAt: string | null = null;
    if (expiresLocal !== '') {
      const parsed = new Date(expiresLocal);
      if (parsed.getTime() <= Date.now()) {
        setExpiryError('Expiry must be in the future.');
        return;
      }
      expiresAt = parsed.toISOString();
    }

    const trimmedLabel = label.trim();
    mutation.mutate({
      label: trimmedLabel === '' ? null : trimmedLabel,
      expires_at: expiresAt,
    });
  };

  return (
    <Dialog
      title="Mint entry code"
      open
      // During the shown-once reveal the code cannot be re-minted, so light
      // dismissal is disabled there — only the explicit Done button closes it.
      // The form phase stays an ordinary dismissable modal.
      dismissable={minted === null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {minted !== null && chatUrl !== null ? (
        <div style={sectionStyle}>
          <CopyField
            value={minted.code}
            label="Entry code"
            caption="This code is shown once — it cannot be shown again. Revoke and mint to get a new one."
          />
          <CopyField
            value={chatUrl}
            label="Chat link"
            caption="Anyone who opens this link enters the gated route."
          />
          <p style={captionStyle}>
            {minted.expires_at === null
              ? 'Never expires.'
              : `Expires ${formatInstant(minted.expires_at)}`}
          </p>
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button type="button" variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      ) : (
        <form aria-label="Mint entry code" onSubmit={onSubmit} style={sectionStyle}>
          <Field label="Label" description="Optional. A human tag to tell codes apart.">
            <TextInput
              value={label}
              placeholder="e.g. newsletter"
              onChange={(event) => {
                setLabel(event.target.value);
              }}
            />
          </Field>
          <Field
            label="Expiry"
            description="Optional. Leave blank for a code that never expires."
            error={expiryError ?? undefined}
          >
            <TextInput
              type="datetime-local"
              value={expiresLocal}
              onChange={(event) => {
                setExpiresLocal(event.target.value);
              }}
            />
          </Field>
          {mutation.isError ? <ErrorState message={errorMessage(mutation.error)} /> : null}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--tai-space-3)' }}>
            <Button type="button" onClick={onClose} disabled={mutation.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={mutation.isPending}>
              {mutation.isPending ? <Spinner label="Minting" /> : null}
              Mint code
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

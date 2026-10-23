/**
 * The words panel on a web route's drill-in: a web route can set its own words for the
 * chat page's refusal pages (`page`) and for the sentences the chat widget draws when a
 * request is refused (`transport`). The panel draws one input per member, filled with the
 * route's stored words; an empty input is a member left unset, which draws the built-in
 * English. Save replaces the route's whole words object with the set inputs, and is off
 * while no input is set (the door refuses empty words); Clear deletes it after a confirm. Each of the three words doors surfaces its failures LOUDLY here: a
 * save the door refuses for one member shows on that member's input, every other failure
 * as an error.
 *
 * Where the web channel's routes are not mounted on this deployment the panel makes no
 * request and says so. The panel holds its own query cache with the host's rules (no
 * retry, a 401 routes to sign-in) and renders only SDK components and tokens, so it themes
 * with the host in both themes.
 */
import {
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  errorMessage,
  ErrorState,
  Field,
  type RoutePanelProps,
  Skeleton,
  TextInput,
  useApi,
  useOnUnauthorized,
} from '@tai42/studio-sdk';
import { QueryClientProvider, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { type CSSProperties, type ReactNode, useRef, useState } from 'react';

import { wordsKey } from './keys';
import { getMountBase } from './mount-base';
import { panelQueryClient } from './query-client';
import { clearWords, getWords, replaceWords } from './words-client';
import {
  PAGE_MEMBERS,
  type PageMember,
  type RefusalPageWords,
  TRANSPORT_MEMBERS,
  type TransportMember,
  type WebWords,
} from './words-schemas';

const headerStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 'var(--tai-space-3)',
  margin: '0 0 var(--tai-space-4)',
};

const bodyStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--tai-space-4)',
};

const actionsStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--tai-space-3)',
};

const titleStyle: CSSProperties = { margin: 0, fontSize: 'var(--tai-text-lg)' };
const groupTitleStyle: CSSProperties = { margin: 0, fontSize: 'var(--tai-text-md)' };
const noteStyle: CSSProperties = { margin: 0, color: 'var(--tai-color-text-muted)' };

const NO_WORDS_NOTE =
  'No words are set for this route, so every page and sentence draws the built-in English.';

/** One input's value per member, keyed by the member's path in the words object
 *  (`page.<member>.heading`, `page.<member>.text`, `transport.<member>`) — the same path a
 *  refused save names. An empty string is a member left unset. */
type Drafts = Readonly<Record<string, string>>;

function headingPath(member: PageMember): string {
  return `page.${member}.heading`;
}

function textPath(member: PageMember): string {
  return `page.${member}.text`;
}

function transportPath(member: TransportMember): string {
  return `transport.${member}`;
}

/** Every member path an input edits. */
const MEMBER_PATHS: ReadonlySet<string> = new Set([
  ...PAGE_MEMBERS.flatMap(({ name }) => [headingPath(name), textPath(name)]),
  ...TRANSPORT_MEMBERS.map(({ name }) => transportPath(name)),
]);

/** The inputs' values for stored words (`null`: none stored). */
function draftsOf(words: WebWords | null): Drafts {
  const drafts: Record<string, string> = {};
  for (const { name } of PAGE_MEMBERS) {
    const page = words?.page?.[name];
    drafts[headingPath(name)] = page?.heading ?? '';
    drafts[textPath(name)] = page?.text ?? '';
  }
  for (const { name } of TRANSPORT_MEMBERS) {
    drafts[transportPath(name)] = words?.transport?.[name] ?? '';
  }
  return drafts;
}

/** The words object the set inputs make: only the members with a value. */
function wordsOf(drafts: Drafts): WebWords {
  const page: Partial<Record<PageMember, RefusalPageWords>> = {};
  for (const { name } of PAGE_MEMBERS) {
    const heading = drafts[headingPath(name)] ?? '';
    const text = drafts[textPath(name)] ?? '';
    if (heading === '' && text === '') continue;
    page[name] = { ...(heading === '' ? {} : { heading }), ...(text === '' ? {} : { text }) };
  }
  const transport: Partial<Record<TransportMember, string>> = {};
  for (const { name } of TRANSPORT_MEMBERS) {
    const sentence = drafts[transportPath(name)] ?? '';
    if (sentence !== '') transport[name] = sentence;
  }
  return {
    ...(Object.keys(page).length === 0 ? {} : { page }),
    ...(Object.keys(transport).length === 0 ? {} : { transport }),
  };
}

/** The member a refused save names and the door's reason, when the refusal is one
 *  member's (`invalid request body: <member path>: <reason>`); `null` for any other
 *  failure. */
function memberRefusal(error: unknown): { path: string; reason: string } | null {
  if (!(error instanceof Error) || error.name !== 'ApiError') return null;
  if ((error as Error & { status?: unknown }).status !== 422) return null;
  const match = /^invalid request body: ([^:]+): (.+)$/s.exec(error.message);
  if (match === null) return null;
  const [, path, reason] = match;
  if (path === undefined || reason === undefined || !MEMBER_PATHS.has(path)) return null;
  return { path, reason };
}

/** The card shown where the web channel's routes are not mounted on this deployment. */
function WordsUnavailable(): ReactNode {
  return (
    <Card>
      <div style={headerStyle}>
        <h2 style={titleStyle}>Words</h2>
      </div>
      <EmptyState
        title="Words unavailable"
        description="The web channel's routes are not mounted on this deployment, so a route's words cannot be read or changed here. Load the web channel to manage them."
      />
    </Card>
  );
}

export function WordsPanel({ route }: RoutePanelProps): ReactNode {
  const base = getMountBase();
  // The panel's own query cache with the host's rules (no retry; a 401 routes to sign-in),
  // held for the life of the mounted panel. The ref keeps the current sign-in handler.
  const onUnauthorized = useOnUnauthorized();
  const signIn = useRef(onUnauthorized);
  signIn.current = onUnauthorized;
  const [queryClient] = useState(() =>
    panelQueryClient(() => {
      signIn.current();
    }),
  );
  if (base === null) return <WordsUnavailable />;
  return (
    <QueryClientProvider client={queryClient}>
      <WordsSection base={base} identity={route.our_identity} />
    </QueryClientProvider>
  );
}

function WordsSection({
  base,
  identity,
}: {
  readonly base: string;
  readonly identity: string;
}): ReactNode {
  const api = useApi();
  const words = useQuery({
    queryKey: wordsKey(identity),
    queryFn: ({ signal }) => getWords(api.request, base, identity, signal),
  });

  let body: ReactNode;
  if (words.isPending) {
    body = <Skeleton height={240} />;
  } else if (words.isError) {
    body = <ErrorState message={errorMessage(words.error)} onRetry={() => void words.refetch()} />;
  } else {
    body = <WordsForm base={base} identity={identity} stored={words.data} />;
  }

  return (
    <Card>
      <div style={headerStyle}>
        <h2 style={titleStyle}>Words</h2>
      </div>
      {body}
    </Card>
  );
}

function WordsForm({
  base,
  identity,
  stored,
}: {
  readonly base: string;
  readonly identity: string;
  /** The words the door holds now; `null` when none are set. */
  readonly stored: WebWords | null;
}): ReactNode {
  const api = useApi();
  const queryClient = useQueryClient();
  const [drafts, setDrafts] = useState<Drafts>(() => draftsOf(stored));
  const [memberErrors, setMemberErrors] = useState<Readonly<Record<string, string>>>({});
  const [saved, setSaved] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);

  const save = useMutation({
    mutationFn: (next: WebWords) => replaceWords(api.request, base, identity, next),
    onSuccess: (answer) => {
      queryClient.setQueryData(wordsKey(identity), answer);
      setDrafts(draftsOf(answer));
      setSaved(true);
    },
    onError: (error) => {
      const refusal = memberRefusal(error);
      if (refusal !== null) setMemberErrors({ [refusal.path]: refusal.reason });
    },
  });

  const clear = useMutation({
    mutationFn: () => clearWords(api.request, base, identity),
    onSuccess: () => {
      queryClient.setQueryData(wordsKey(identity), null);
      setDrafts(draftsOf(null));
      setMemberErrors({});
      setSaved(false);
      setConfirmClear(false);
    },
  });

  const edit = (path: string, value: string): void => {
    setDrafts((current) => ({ ...current, [path]: value }));
    setSaved(false);
    setMemberErrors((current) => {
      if (!(path in current)) return current;
      const { [path]: _dropped, ...rest } = current;
      return rest;
    });
  };

  const input = (path: string, label: string, description?: string): ReactNode => (
    <Field
      key={path}
      label={label}
      {...(description === undefined ? {} : { description })}
      {...(memberErrors[path] === undefined ? {} : { error: memberErrors[path] })}
    >
      <TextInput
        value={drafts[path] ?? ''}
        onChange={(event) => {
          edit(path, event.target.value);
        }}
      />
    </Field>
  );

  const next = wordsOf(drafts);
  const nothingSet = next.page === undefined && next.transport === undefined;

  return (
    <div style={bodyStyle}>
      {stored === null ? <p style={noteStyle}>{NO_WORDS_NOTE}</p> : null}
      <h3 style={groupTitleStyle}>Refusal pages</h3>
      <p style={noteStyle}>
        The pages the chat link serves in place of the chat when it refuses a visit.
      </p>
      {PAGE_MEMBERS.map(({ name, label, description }) => [
        input(headingPath(name), `${label}: heading`, description),
        input(textPath(name), `${label}: text`),
      ])}
      <h3 style={groupTitleStyle}>Chat sentences</h3>
      <p style={noteStyle}>The sentences the chat draws when one of its requests is refused.</p>
      {TRANSPORT_MEMBERS.map(({ name, label, description }) =>
        input(transportPath(name), label, description),
      )}
      {save.isError && memberRefusal(save.error) === null ? (
        <ErrorState message={errorMessage(save.error)} />
      ) : null}
      <div style={actionsStyle}>
        <Button
          variant="primary"
          disabled={save.isPending || nothingSet}
          onClick={() => {
            setMemberErrors({});
            setSaved(false);
            save.mutate(next);
          }}
        >
          Save words
        </Button>
        {stored === null ? null : (
          <Button
            variant="danger"
            onClick={() => {
              clear.reset();
              setConfirmClear(true);
            }}
          >
            Clear words
          </Button>
        )}
        {saved ? <span role="status">Words saved.</span> : null}
      </div>
      {confirmClear ? (
        <ConfirmDialog
          title="Clear the words"
          confirmLabel="Clear words"
          pendingLabel="Clearing"
          confirmVariant="danger"
          isPending={clear.isPending}
          error={clear.isError ? clear.error : null}
          onConfirm={() => {
            clear.mutate();
          }}
          onClose={() => {
            clear.reset();
            setConfirmClear(false);
          }}
        >
          <p style={{ margin: 0 }}>
            Every page and sentence of this route will draw the built-in English again.
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

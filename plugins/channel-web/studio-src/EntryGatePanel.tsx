/**
 * The entry-gate panel on a web route's drill-in, the web channel's route-panel
 * contribution: a web route can be gated so its chat page is served only to a navigation
 * carrying a live entry code. The panel reads and changes the gate through the four gate
 * doors, each surfacing its failures LOUDLY here.
 *
 * Where the web channel's routes are not mounted on this deployment the panel makes no
 * request and says so. The panel holds its own query cache (the host's is not shared
 * across the plugin boundary) and renders only SDK components and tokens, so it themes
 * with the host in both themes.
 */
import {
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  EmptyState,
  errorMessage,
  ErrorState,
  type RoutePanelProps,
  Skeleton,
} from '@tai42/studio-sdk';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type CSSProperties, type ReactNode, useState } from 'react';

import { EntryCodesTable } from './EntryCodesTable';
import { getGateBase } from './gate-base';
import { MintEntryCodeDialog } from './MintEntryCodeDialog';
import { RevokeCodeDialog } from './RevokeCodeDialog';
import { useEntryGate } from './useEntryGate';

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

const titleStyle: CSSProperties = { margin: 0, fontSize: 'var(--tai-text-lg)' };

/** The card shown where the web channel's routes are not mounted on this deployment. */
function GateUnavailable(): ReactNode {
  return (
    <Card>
      <div style={headerStyle}>
        <h2 style={titleStyle}>Entry gate</h2>
      </div>
      <EmptyState
        title="Entry gate unavailable"
        description="The web channel's routes are not mounted on this deployment, so its entry gate cannot be read or changed here. Load the web channel to manage entry codes."
      />
    </Card>
  );
}

export function EntryGatePanel({ route }: RoutePanelProps): ReactNode {
  const base = getGateBase();
  // The panel's own query cache, held for the life of the mounted panel.
  const [queryClient] = useState(() => new QueryClient());
  if (base === null) return <GateUnavailable />;
  return (
    <QueryClientProvider client={queryClient}>
      <GatePanel base={base} identity={route.our_identity} />
    </QueryClientProvider>
  );
}

function GatePanel({
  base,
  identity,
}: {
  readonly base: string;
  readonly identity: string;
}): ReactNode {
  const [minting, setMinting] = useState(false);
  const {
    gate,
    toggleMutation,
    revokeMutation,
    onToggle,
    confirmEnable,
    setConfirmEnable,
    pendingRevoke,
    setPendingRevoke,
  } = useEntryGate(base, identity);

  let body: ReactNode;
  if (gate.isPending) {
    body = <Skeleton height={120} />;
  } else if (gate.isError) {
    body = <ErrorState message={errorMessage(gate.error)} onRetry={() => void gate.refetch()} />;
  } else {
    body = (
      <div style={bodyStyle}>
        <Checkbox
          label="Require an entry code"
          checked={gate.data.enabled}
          disabled={toggleMutation.isPending}
          onCheckedChange={onToggle}
        />
        {toggleMutation.isError && !confirmEnable ? (
          <ErrorState message={errorMessage(toggleMutation.error)} />
        ) : null}
        <EntryCodesTable
          codes={gate.data.codes}
          onRevoke={(codeId) => {
            revokeMutation.reset();
            setPendingRevoke(codeId);
          }}
        />
      </div>
    );
  }

  return (
    <Card>
      <div style={headerStyle}>
        <h2 style={titleStyle}>Entry gate</h2>
        <Button
          variant="primary"
          onClick={() => {
            setMinting(true);
          }}
        >
          Mint entry code
        </Button>
      </div>
      {body}
      {minting ? (
        <MintEntryCodeDialog
          base={base}
          identity={identity}
          onClose={() => {
            setMinting(false);
          }}
        />
      ) : null}
      {confirmEnable ? (
        <ConfirmDialog
          title="Turn on the entry gate"
          confirmLabel="Turn on anyway"
          pendingLabel="Turning on"
          confirmVariant="primary"
          isPending={toggleMutation.isPending}
          error={toggleMutation.isError ? toggleMutation.error : null}
          onConfirm={() => {
            toggleMutation.mutate(true);
          }}
          onClose={() => {
            toggleMutation.reset();
            setConfirmEnable(false);
          }}
        >
          <p style={{ margin: 0 }}>
            No live codes exist, so nobody can enter this route until you mint one. Turn the gate on
            anyway?
          </p>
        </ConfirmDialog>
      ) : null}
      {pendingRevoke !== null ? (
        <RevokeCodeDialog
          isError={revokeMutation.isError}
          error={revokeMutation.error}
          isPending={revokeMutation.isPending}
          onCancel={() => {
            setPendingRevoke(null);
          }}
          onConfirm={() => {
            revokeMutation.mutate(pendingRevoke);
          }}
        />
      ) : null}
    </Card>
  );
}

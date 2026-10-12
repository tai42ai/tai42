/**
 * The entry-code list for a gated web route: an empty state to prompt minting, or a
 * table of live codes (label, created, expiry) each with a Revoke action.
 */
import {
  Button,
  EmptyState,
  ScrollRegion,
  Table,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from '@tai42/studio-sdk';
import type { ReactNode } from 'react';

import { EMPTY_PLACEHOLDER, formatInstant } from './format';

/** One minted entry code, as the gate read returns it. */
interface EntryCode {
  readonly code_id: string;
  readonly label: string | null;
  readonly created_at: string;
  readonly expires_at: string | null;
}

/** The expiry cell: "Never" for a null expiry, else the locale-formatted instant. */
function formatExpiry(value: string | null): string {
  return value === null ? 'Never' : formatInstant(value);
}

export function EntryCodesTable({
  codes,
  onRevoke,
}: {
  readonly codes: readonly EntryCode[];
  readonly onRevoke: (codeId: string) => void;
}): ReactNode {
  if (codes.length === 0) {
    return (
      <EmptyState
        title="No entry codes"
        description="Mint a code to hand out — anyone who opens its chat link enters the gated route."
      />
    );
  }
  return (
    <ScrollRegion label="Entry codes">
      <Table>
        <THead>
          <TR>
            <TH>Label</TH>
            <TH>Created</TH>
            <TH>Expires</TH>
            <TH aria-label="Actions" />
          </TR>
        </THead>
        <TBody>
          {codes.map((code) => (
            <TR key={code.code_id}>
              <TD>{code.label ?? EMPTY_PLACEHOLDER}</TD>
              <TD>{formatInstant(code.created_at)}</TD>
              <TD>{formatExpiry(code.expires_at)}</TD>
              <TD style={{ textAlign: 'right' }}>
                <Button
                  variant="ghost"
                  aria-label={`Revoke entry code ${code.label ?? code.code_id}`}
                  onClick={() => {
                    onRevoke(code.code_id);
                  }}
                >
                  Revoke
                </Button>
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </ScrollRegion>
  );
}

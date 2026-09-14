/**
 * The one-time invite link, shown once with its shown-once warning. Shared by the
 * create flow and invite regeneration.
 */
import type { ReactElement } from 'react';
import { CopyField } from '@tai42/studio-sdk';
import type { InviteResult } from '@/api';

export function InviteResultView({ result }: { result: InviteResult }): ReactElement {
  return (
    <div className="users-dialog-body">
      <CopyField
        label="Invite link"
        value={result.login_path}
        caption="Copy this link now — it is shown only once. Send it to the user; opening it lets them set a password and sign in."
      />
    </div>
  );
}

/**
 * Runs once before a run against a target: asks the target which kinds it has on
 * (`GET /api/system/kinds` with the login key) and hands the answer to the workers,
 * where each spec's declared needs are matched against it.
 */
import { KINDS_ENV, TARGET } from './target';

interface KindRow {
  kind: string;
  state: string;
  plugin: string | null;
  detail: string | null;
}

export default async function globalSetup(): Promise<void> {
  if (!TARGET) return;
  const url = `${TARGET.url}/api/system/kinds`;
  const response = await fetch(url, {
    headers: TARGET.key ? { authorization: `Bearer ${TARGET.key}` } : {},
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error(
      `the e2e target refused ${url} (${String(response.status)}): set the login key in $${TARGET.keyEnv}`,
    );
  }
  if (!response.ok) {
    throw new Error(`the e2e target answered ${url} with ${String(response.status)}`);
  }
  const body = (await response.json()) as { data: KindRow[] };
  const kinds: Record<string, string> = {};
  for (const row of body.data) {
    if (row.state !== 'off')
      kinds[row.kind] = `${row.plugin ?? ''} ${row.detail ?? ''}`.toLowerCase();
  }
  process.env[KINDS_ENV] = JSON.stringify(kinds);
}

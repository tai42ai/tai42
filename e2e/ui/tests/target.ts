/**
 * The e2e target: a running stack this suite drives instead of building one.
 *
 * `TAI_E2E_TARGET` names it — a bare origin (`https://stack.example.com`) or a target
 * file (`staging` resolves to `e2e/targets/staging.yml`; a value ending in `.yml` is a
 * path). A target file holds the origin, the NAME of the environment variable carrying
 * the login key, and the facts the stack cannot report about itself; which kinds it has
 * on is read from its `GET /api/system/kinds` by the global setup. Unset or empty, the
 * suite builds its own stack and `TARGET` is `undefined`.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { parse } from 'yaml';

/** Facts a target file may list under `provides`. */
export const DECLARED_NEEDS: Readonly<Record<string, string>> = {
  'probe-tools': 'the e2e probe tools are loaded in the stack',
  mutable: 'tests may change stack-wide state',
};

/** Needs only a stack the run builds itself can meet. */
export const BUILT_NEEDS: Readonly<Record<string, string>> = {
  process: "control of the stack's processes",
  store: "direct access to the stack's Redis or Postgres",
  files: "the stack's files on disk",
  helper: 'a local helper service the test controls',
  setting: 'a specific setting of the stack',
  topology: 'a specific process topology',
  cli: 'the CLI run beside the stack',
  metrics: 'the standalone metrics process',
  'second-stack': 'a second stack',
  'fixture-page': "the suite's own fixture page",
  'no-stack': 'no running stack at all',
};

export interface Target {
  /** The stack's origin, without a trailing slash. */
  url: string;
  /** The login key, read from the environment variable the target names. */
  key: string | undefined;
  /** The name of that environment variable. */
  keyEnv: string;
  /** The declared facts (`provides`). */
  provides: readonly string[];
  /** The kinds the stack reports as on, each mapped to its plugin + detail text. */
  kinds: Readonly<Record<string, string>> | undefined;
}

const DEFAULT_KEY_ENV = 'TAI_E2E_KEY';
/** Where the global setup hands the fetched kinds to the workers. */
export const KINDS_ENV = 'TAI_E2E_TARGET_KINDS';
const TARGETS_DIR = fileURLToPath(new URL('../../targets/', import.meta.url));

function origin(value: string, source: string): string {
  const trimmed = value.trim().replace(/\/+$/, '');
  const parsed = URL.canParse(trimmed) ? new URL(trimmed) : undefined;
  const bare =
    parsed !== undefined &&
    (parsed.protocol === 'http:' || parsed.protocol === 'https:') &&
    parsed.pathname === '/' &&
    parsed.search === '' &&
    parsed.hash === '' &&
    parsed.username === '' &&
    parsed.password === '';
  if (!bare) {
    throw new Error(`${source}: expected an http(s) origin like https://host:port, got "${value}"`);
  }
  return trimmed;
}

function resolveTarget(): Target | undefined {
  const value = process.env.TAI_E2E_TARGET?.trim();
  if (!value) return undefined;
  const kindsJson = process.env[KINDS_ENV];
  const kinds = kindsJson ? (JSON.parse(kindsJson) as Record<string, string>) : undefined;
  if (/^https?:\/\//.test(value)) {
    return {
      url: origin(value, 'TAI_E2E_TARGET'),
      key: process.env[DEFAULT_KEY_ENV] || undefined,
      keyEnv: DEFAULT_KEY_ENV,
      provides: [],
      kinds,
    };
  }
  const path = value.endsWith('.yml') ? value : `${TARGETS_DIR}${value}.yml`;
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch {
    throw new Error(`TAI_E2E_TARGET="${value}": no target file at ${path}`);
  }
  const document: unknown = parse(text);
  if (typeof document !== 'object' || document === null || !('url' in document)) {
    throw new Error(`${path}: a target file is a mapping with a \`url\``);
  }
  const fields = document as Record<string, unknown>;
  const unknownKeys = Object.keys(fields).filter(
    (k) => !['url', 'key_env', 'provides'].includes(k),
  );
  if (unknownKeys.length > 0) throw new Error(`${path}: unknown key(s) ${unknownKeys.join(', ')}`);
  const provides = fields.provides ?? [];
  if (!Array.isArray(provides)) throw new Error(`${path}: \`provides\` is a list`);
  const unknownFacts = provides.map(String).filter((fact) => !(fact in DECLARED_NEEDS));
  if (unknownFacts.length > 0) {
    throw new Error(
      `${path}: \`provides\` lists unknown fact(s) ${unknownFacts.join(', ')}; a target can declare ${Object.keys(DECLARED_NEEDS).join(', ')}`,
    );
  }
  const keyEnv =
    typeof fields.key_env === 'string' && fields.key_env ? fields.key_env : DEFAULT_KEY_ENV;
  return {
    url: origin(String(fields.url), path),
    key: process.env[keyEnv] || undefined,
    keyEnv,
    provides: provides.map(String),
    kinds,
  };
}

/** The target this run drives, or `undefined` when the suite builds its own stack. */
export const TARGET = resolveTarget();

/**
 * The spec files that declare no needs. They are built-stack only, so a run against a
 * target leaves them out.
 */
export function undeclaredSpecs(): string[] {
  const dir = fileURLToPath(new URL('./', import.meta.url));
  return readdirSync(dir)
    .filter((name) => name.endsWith('.spec.ts'))
    .filter((name) => !/^\s*needs\(/m.test(readFileSync(`${dir}${name}`, 'utf8')))
    .map((name) => `**/${name}`);
}

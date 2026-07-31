/**
 * Bind a topic verifier (Mission 9 PLAN_1 UI). Via the hooks page: bind the
 * `github` catalog verifier to a fresh topic, pointing it at the stack's secret
 * env var. Asserts the bound state in the UI and via `GET /api/hooks`
 * (`data.topic_verifiers`), with the sharp negative: an UNSIGNED delivery to the
 * bound topic is now rejected, while an unbound topic stays open by design.
 */
import { expect, test } from '@playwright/test';
import { apiHeaders, seedCredential, uniq } from './helpers';

// Topic paths route through `/universal_webhook/{topic}`; keep them path-safe.
function topicName(): string {
  return `topic-${Math.random().toString(16).slice(2, 10)}`;
}

test('bind the github verifier to a topic; UI + API + unsigned rejection', async ({ page, request }) => {
  const topic = topicName();
  const unboundTopic = topicName();

  await seedCredential(page);
  await page.goto('/hooks');

  // Bind: topic + verifier `github` + the config naming the secret ENV VAR (the
  // secret value itself never leaves the environment).
  const form = page.getByRole('form', { name: 'Bind topic verifier' });
  // The Topic field is an ``<input list=…>`` (a datalist-backed autocomplete),
  // which exposes the ARIA ``combobox`` role — not ``textbox`` — but still fills.
  await form.getByRole('combobox', { name: 'Topic' }).fill(topic);
  await form.getByRole('combobox', { name: 'Verifier' }).click();
  await page.getByRole('option', { name: 'github' }).click();
  await form.getByRole('textbox', { name: 'Config (JSON)' }).fill(JSON.stringify({ secret_env: 'E2E_GH_WEBHOOK_SECRET' }));
  await form.getByRole('button', { name: 'Bind verifier' }).click();

  // UI: the bound row shows the topic verified by `github` (the per-row unbind
  // control names the topic).
  await expect(page.getByRole('button', { name: `Unbind verifier from ${topic}` })).toBeVisible();

  // API: the binding is carried under `data.topic_verifiers`.
  const hooks = await request.get('/api/hooks', { headers: apiHeaders() });
  expect(hooks.status(), await hooks.text()).toBe(200);
  const body = (await hooks.json()) as { data: { topic_verifiers: Record<string, { verifier: string }> } };
  expect(body.data.topic_verifiers[topic]?.verifier).toBe('github');

  // Sharp negative: an UNSIGNED POST to the BOUND topic is rejected (the github
  // verifier is body-signature; a missing signature fails verification → 401).
  const signed = await request.post(`/universal_webhook/${topic}`, {
    headers: { 'content-type': 'application/json' },
    data: { delivery: 'unsigned' },
  });
  expect(signed.status()).toBe(401);

  // An UNBOUND topic stays OPEN by design (no verifier ⇒ accepted).
  const open = await request.post(`/universal_webhook/${unboundTopic}`, {
    headers: { 'content-type': 'application/json' },
    data: { delivery: 'unsigned' },
  });
  expect(open.status()).toBe(200);
});

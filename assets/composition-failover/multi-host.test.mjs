import assert from 'node:assert/strict';
import test from 'node:test';
import worker from './multi-host.mjs';

const settings = {
  APEX_ZONE: 'example.com',
  APEX_PUBLIC_HOSTS: JSON.stringify({ 'sso.example.com': '204', 'app.example.com': '200' }),
  APEX_ORIGIN_PRIMARY: 'one.example.com',
  APEX_ORIGIN_SECONDARY: 'two.example.com',
  APEX_READY_PATH: '/.well-known/apex-ready',
  APEX_READY_TOKEN: 'synthetic-private-token',
};

test('each allowed hostname uses its own health status and preserves the application request', async () => {
  const original = globalThis.fetch;
  try {
    for (const [host, health] of [['sso.example.com', 204], ['app.example.com', 200]]) {
      const calls = [];
      globalThis.fetch = async (request) => {
        calls.push(request);
        return calls.length === 1 ? new Response(null, { status: health }) : new Response('accepted');
      };
      const result = await worker.fetch(new Request(`https://${host}/callback`, { method: 'POST', body: 'one-use-body' }), settings);
      assert.equal(result.status, 200);
      assert.equal(calls.length, 2);
      assert.equal(new URL(calls[0].url).hostname, host);
      assert.equal(calls[1].method, 'POST');
      assert.equal(await calls[1].text(), 'one-use-body');
    }
  } finally { globalThis.fetch = original; }
});

test('unknown hosts do not probe or dispatch', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('unexpected dispatch'); };
  try {
    const response = await worker.fetch(new Request('https://other.example.com/'), settings);
    assert.equal(response.status, 421);
  } finally { globalThis.fetch = original; }
});

test('missing and malformed host policies fail closed before dispatch', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response('unexpected'); };
  try {
    for (const value of [undefined, '{', '[]', '{}', '{"app.example.com":"302"}', '{"APP.example.com":"200"}']) {
      const response = await worker.fetch(new Request('https://app.example.com/'), { ...settings, APEX_PUBLIC_HOSTS: value });
      assert.equal(response.status, 503);
    }
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

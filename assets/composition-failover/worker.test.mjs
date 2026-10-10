import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker from './worker.mjs';

const env = {
  APEX_PUBLIC_HOST: 'sso.example.com',
  APEX_ZONE: 'example.com',
  APEX_ORIGIN_PRIMARY: 'primary.example.com',
  APEX_ORIGIN_SECONDARY: 'secondary.example.com',
  APEX_READY_PATH: '/.well-known/apex-ready',
  APEX_READY_TOKEN: 'test-only-readiness-secret',
};
const url = 'https://sso.example.com/api/callback?state=a%2Fb';
const ready = () => new Response(null, { status: 204 });

function intercept(t, handler) {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (request, options) => {
    calls.push({ request, options });
    return handler(request, options, calls.length);
  });
  return calls;
}

function checkProbe(call, origin) {
  assert.equal(call.request.url, 'https://sso.example.com/.well-known/apex-ready');
  assert.equal(call.request.method, 'GET');
  assert.equal(call.request.headers.get('X-APEX-Ready-Token'), env.APEX_READY_TOKEN);
  assert.equal(call.request.headers.get('Cookie'), null);
  assert.equal(call.request.headers.get('Authorization'), null);
  assert.equal(call.options.cf.resolveOverride, origin);
  assert.equal(call.options.redirect, 'manual');
  assert.equal(call.options.cache, 'no-store');
  assert.deepEqual(call.options.cf.cacheTtlByStatus, { '100-599': -1 });
}

test('ready primary receives the original POST exactly once with credentials and body', async (t) => {
  const calls = intercept(t, async (request, options, count) => {
    if (count === 1) return ready();
    assert.equal(await request.text(), 'code=one-use&state=a%2Fb');
    return new Response('accepted', { status: 201 });
  });
  const request = new Request(url, {
    method: 'POST', body: 'code=one-use&state=a%2Fb',
    headers: { Cookie: 'session=original', Authorization: 'Bearer original',
      'X-APEX-Ready-Token': 'client-supplied', 'Content-Type': 'application/x-www-form-urlencoded' },
  });
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 201);
  assert.equal(await response.text(), 'accepted');
  assert.equal(calls.length, 2);
  checkProbe(calls[0], env.APEX_ORIGIN_PRIMARY);
  const forwarded = calls[1];
  assert.equal(forwarded.request.url, url);
  assert.equal(forwarded.request.method, 'POST');
  assert.equal(forwarded.request.headers.get('Cookie'), 'session=original');
  assert.equal(forwarded.request.headers.get('Authorization'), 'Bearer original');
  assert.equal(forwarded.request.headers.get('X-APEX-Ready-Token'), null);
  assert.equal(forwarded.options.cf.resolveOverride, env.APEX_ORIGIN_PRIMARY);
  assert.equal(forwarded.options.redirect, 'manual');
  assert.equal(forwarded.options.cache, 'no-store');
  assert.match(response.headers.get('Cache-Control'), /no-store/);
});

test('unready primary cancels unread body and selects ready secondary', async (t) => {
  let canceled = false;
  const calls = intercept(t, (_, __, count) => count === 1
    ? new Response(new ReadableStream({ cancel() { canceled = true; } }), { status: 503 })
    : count === 2 ? ready() : new Response('secondary'));
  const response = await worker.fetch(new Request(url), env);
  assert.equal(await response.text(), 'secondary');
  assert.equal(canceled, true);
  assert.equal(calls.length, 3);
  checkProbe(calls[1], env.APEX_ORIGIN_SECONDARY);
  assert.equal(calls[2].options.cf.resolveOverride, env.APEX_ORIGIN_SECONDARY);
});

test('probe deadline aborts at two seconds even if fetch ignores cancellation', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let finishPrimary;
  let canceled = false;
  const calls = intercept(t, (_, __, count) => count === 1
    ? new Promise((resolve) => { finishPrimary = resolve; })
    : count === 2 ? ready() : new Response('secondary'));
  const pending = worker.fetch(new Request(url), env);
  await Promise.resolve();
  t.mock.timers.tick(1999);
  assert.equal(calls.length, 1);
  t.mock.timers.tick(1);
  const response = await pending;
  assert.equal(await response.text(), 'secondary');
  assert.equal(calls[0].options.signal.aborted, true);
  finishPrimary(new Response(new ReadableStream({ cancel() { canceled = true; } })));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(canceled, true);
  assert.equal(calls.length, 3);
});

test('two unavailable origins return no-store 503 without client dispatch', async (t) => {
  const calls = intercept(t, () => new Response('down', { status: 503 }));
  const response = await worker.fetch(new Request(url), env);
  assert.equal(response.status, 503);
  assert.match(response.headers.get('Cache-Control'), /no-store/);
  assert.equal(calls.length, 2);
  assert.ok(calls.every(({ request }) => new URL(request.url).pathname === env.APEX_READY_PATH));
});

test('readiness redirect is never accepted or followed', async (t) => {
  const calls = intercept(t, (_, __, count) => count === 1
    ? new Response(null, { status: 302, headers: { Location: 'https://evil.example/' } })
    : count === 2 ? ready() : new Response('secondary'));
  assert.equal(await (await worker.fetch(new Request(url), env)).text(), 'secondary');
  assert.equal(calls.length, 3);
  assert.equal(calls[0].options.redirect, 'manual');
});

for (const method of ['GET', 'POST']) {
  test(`failed ${method} dispatch is never replayed on another origin`, async (t) => {
    const calls = intercept(t, (_, __, count) => {
      if (count === 1) return ready();
      throw new DOMException('origin timed out', 'TimeoutError');
    });
    const response = await worker.fetch(new Request(url, { method,
      ...(method === 'POST' ? { body: 'one-use' } : {}) }), env);
    assert.equal(response.status, 502);
    assert.match(response.headers.get('Cache-Control'), /no-store/);
    assert.equal(calls.length, 2);
  });
}

test('origin errors preserve status and body without probing secondary', async (t) => {
  const calls = intercept(t, (_, __, count) => count === 1 ? ready() : new Response('origin error', { status: 500 }));
  const response = await worker.fetch(new Request(url), env);
  assert.equal(response.status, 500);
  assert.equal(await response.text(), 'origin error');
  assert.equal(calls.length, 2);
});

test('auth redirect retains distinct Set-Cookie headers and Location but cannot cache', async (t) => {
  const cookies = ['session=first; Secure; HttpOnly', 'state=second; Expires=Wed, 21 Oct 2030 07:28:00 GMT'];
  const headers = new Headers({ Location: '/continue?state=a%2Fb', 'Cache-Control': 'public, max-age=3600' });
  for (const cookie of cookies) headers.append('Set-Cookie', cookie);
  const calls = intercept(t, (_, __, count) => count === 1 ? ready() : new Response(null, { status: 302, headers }));
  const response = await worker.fetch(new Request(url), env);
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('Location'), '/continue?state=a%2Fb');
  assert.deepEqual(response.headers.getSetCookie(), cookies);
  assert.match(response.headers.get('Cache-Control'), /no-store/);
  assert.equal(response.headers.get('Cloudflare-CDN-Cache-Control'), 'no-store');
  assert.equal(calls.length, 2);
});

test('hostile hosts and public readiness requests never reach origins', async (t) => {
  const calls = intercept(t, () => { throw new Error('must not dispatch'); });
  for (const target of ['https://evil.example/', 'https://sso.example.com.evil.example/', 'https://sso.example.com:444/']) {
    assert.equal((await worker.fetch(new Request(target), env)).status, 421);
  }
  for (const path of [env.APEX_READY_PATH, env.APEX_READY_PATH + '?probe=1', '/.well-known/%61pex-ready']) {
    const response = await worker.fetch(new Request('https://sso.example.com' + path), env);
    assert.equal(response.status, 404);
    assert.match(response.headers.get('Cache-Control'), /no-store/);
  }
  assert.equal(calls.length, 0);
});

test('missing and invalid bindings fail closed without diagnostics or probes', async (t) => {
  const calls = intercept(t, () => { throw new Error('must not dispatch'); });
  const invalid = Object.keys(env).map((name) => ({ ...env, [name]: undefined }));
  for (const [name, value] of [
    ['APEX_ZONE', 'com'], ['APEX_PUBLIC_HOST', 'https://sso.example.com'],
    ['APEX_ORIGIN_PRIMARY', 'primary.other.example'], ['APEX_ORIGIN_PRIMARY', env.APEX_PUBLIC_HOST],
    ['APEX_ORIGIN_PRIMARY', env.APEX_ORIGIN_SECONDARY], ['APEX_ORIGIN_SECONDARY', '127.0.0.1'],
    ['APEX_ORIGIN_SECONDARY', '-invalid.example.com'], ['APEX_ORIGIN_PRIMARY', 'a..example.com'],
    ['APEX_READY_PATH', '//evil.example/'], ['APEX_READY_PATH', '/ready?query=yes'],
    ['APEX_READY_PATH', '/ready#fragment'], ['APEX_READY_PATH', '/a/../ready'],
    ['APEX_READY_PATH', '/%72eady'], ['APEX_READY_TOKEN', 'secret\r\ninjected: yes'],
  ]) invalid.push({ ...env, [name]: value });
  invalid.push(null);
  for (const bindings of invalid) {
    const response = await worker.fetch(new Request(url), bindings);
    assert.equal(response.status, 503);
    assert.match(response.headers.get('Cache-Control'), /no-store/);
    assert.equal(await response.text(), 'Service unavailable');
  }
  assert.equal(calls.length, 0);
});

test('response remains streaming without buffering the body', async (t) => {
  let controller;
  const body = new ReadableStream({ start(value) { controller = value; } });
  intercept(t, (_, __, count) => count === 1 ? ready() : new Response(body));
  const response = await worker.fetch(new Request(url), env);
  const reader = response.body.getReader();
  controller.enqueue(new TextEncoder().encode('first'));
  assert.equal(new TextDecoder().decode((await reader.read()).value), 'first');
  controller.close();
  assert.equal((await reader.read()).done, true);
});

test('failed readiness fetch selects secondary without exposing exception details', async (t) => {
  const calls = intercept(t, (_, __, count) => {
    if (count === 1) throw new Error(env.APEX_READY_TOKEN);
    return count === 2 ? ready() : new Response('secondary');
  });
  const response = await worker.fetch(new Request(url), env);
  assert.equal(await response.text(), 'secondary');
  assert.equal(calls.length, 3);
});

test('secondary probe has its own two-second deadline and no client dispatch follows', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const calls = intercept(t, () => new Promise(() => {}));
  const pending = worker.fetch(new Request(url), env);
  t.mock.timers.tick(2000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.signal.aborted, true);
  assert.equal(calls[1].options.signal.aborted, false);
  t.mock.timers.tick(2000);
  const response = await pending;
  assert.equal(response.status, 503);
  assert.equal(calls[1].options.signal.aborted, true);
  assert.equal(calls.length, 2);
});

test('unread probe cancellation cannot stall selection', async (t) => {
  const calls = intercept(t, (_, __, count) => count === 1
    ? new Response(new ReadableStream({ cancel() { return new Promise(() => {}); } }), { status: 500 })
    : count === 2 ? ready() : new Response('secondary'));
  assert.equal(await (await worker.fetch(new Request(url), env)).text(), 'secondary');
  assert.equal(calls.length, 3);
});

test('platform WebSocket upgrade response is returned unchanged', async (t) => {
  // Node cannot construct status 101; the fetch boundary represents Workers' response.
  const upgrade = { status: 101, webSocket: {}, headers: new Headers({ Upgrade: 'websocket' }) };
  const calls = intercept(t, (_, __, count) => count === 1 ? ready() : upgrade);
  const response = await worker.fetch(new Request(url, { headers: { Upgrade: 'websocket' } }), env);
  assert.equal(response, upgrade);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].request.headers.get('Upgrade'), 'websocket');
});

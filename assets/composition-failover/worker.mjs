const READY_HEADER = 'X-APEX-Ready-Token';
const PROBE_TIMEOUT_MS = 2000;

function hostname(value) {
  if (typeof value !== 'string' || value.length > 253 || !value.includes('.')) return null;
  const name = value.toLowerCase();
  if (!name.split('.').every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
  if (/^[0-9.]+$/.test(name)) return null;
  return name;
}

function configuration(env) {
  if (!env) return null;
  const zone = hostname(env.APEX_ZONE);
  const publicHost = hostname(env.APEX_PUBLIC_HOST);
  const primary = hostname(env.APEX_ORIGIN_PRIMARY);
  const secondary = hostname(env.APEX_ORIGIN_SECONDARY);
  if (!zone || ![publicHost, primary, secondary].every((name) =>
    name && (name === zone || name.endsWith('.' + zone)))) return null;
  if (new Set([publicHost, primary, secondary]).size !== 3) return null;
  const path = env.APEX_READY_PATH;
  if (typeof path !== 'string' || !/^\/[a-zA-Z0-9._~/-]*$/.test(path)
      || path.includes('//') || path.split('/').some((part) => part === '.' || part === '..')) return null;
  const token = env.APEX_READY_TOKEN;
  if (typeof token !== 'string' || !/^[\x21-\x7e]+$/.test(token)) return null;
  return { publicHost, primary, secondary, path, token };
}

function unavailable(status = 503, message = 'Service unavailable') {
  return new Response(message, { status, headers: { 'Cache-Control': 'private, no-store' } });
}

function options(origin) {
  return {
    redirect: 'manual',
    cache: 'no-store',
    cf: { resolveOverride: origin, cacheEverything: false, cacheTtlByStatus: { '100-599': -1 } },
  };
}

function discard(response) {
  // Do not let a slow body cancellation extend the probe deadline.
  if (response.body) response.body.cancel().catch(() => {});
}

async function ready(config, origin) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve(false);
    }, PROBE_TIMEOUT_MS);
  });
  try {
    const probe = new Request(`https://${config.publicHost}${config.path}`, {
      headers: { [READY_HEADER]: config.token },
      redirect: 'manual',
      cache: 'no-store',
    });
    const inspection = fetch(probe, { ...options(origin), signal: controller.signal })
      .then((response) => {
        const accepted = !controller.signal.aborted && response.status === 204;
        discard(response);
        return accepted;
      }, () => false);
    return await Promise.race([inspection, deadline]);
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export default {
  async fetch(request, env) {
    const config = configuration(env);
    if (!config) return unavailable();
    const url = new URL(request.url);
    if (url.hostname !== config.publicHost || url.port || url.protocol !== 'https:') {
      return unavailable(421, 'Misdirected request');
    }
    let path;
    try {
      path = decodeURIComponent(url.pathname);
    } catch {
      return unavailable(400, 'Invalid path');
    }
    if (path === config.path) return unavailable(404, 'Not found');

    let origin;
    if (await ready(config, config.primary)) origin = config.primary;
    else if (await ready(config, config.secondary)) origin = config.secondary;
    else return unavailable();

    try {
      const headers = new Headers(request.headers);
      headers.delete(READY_HEADER);
      const forwarded = new Request(request, { headers, redirect: 'manual', cache: 'no-store' });
      // This is the only application dispatch. Its failure never triggers replay.
      const response = await fetch(forwarded, options(origin));
      if (response.status === 101) return response;
      const result = new Response(response.body, response);
      result.headers.set('Cache-Control', 'private, no-store');
      result.headers.set('CDN-Cache-Control', 'no-store');
      result.headers.set('Cloudflare-CDN-Cache-Control', 'no-store');
      return result;
    } catch {
      return unavailable(502, 'Origin request failed');
    }
  },
};

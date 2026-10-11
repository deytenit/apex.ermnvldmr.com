import singleHost from './worker.mjs';

function unavailable(status) {
  return new Response(status === 421 ? 'Misdirected request' : 'Service unavailable', {
    status, headers: { 'Cache-Control': 'private, no-store' },
  });
}

export default {
  async fetch(request, env) {
    let hosts;
    try { hosts = JSON.parse(env?.APEX_PUBLIC_HOSTS); } catch { return unavailable(503); }
    if (!hosts || Array.isArray(hosts) || typeof hosts !== 'object') return unavailable(503);
    const entries = Object.entries(hosts);
    if (!entries.length || entries.length > 64 || entries.some(([name, status]) =>
      name !== name.toLowerCase() || name.length > 253 || !name.includes('.')
      || !name.split('.').every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
      || !['200', '204'].includes(status))) return unavailable(503);
    const host = new URL(request.url).hostname;
    if (!Object.hasOwn(hosts, host)) return unavailable(421);
    return singleHost.fetch(request, { ...env, APEX_PUBLIC_HOST: host, APEX_READY_STATUS: hosts[host] });
  },
};

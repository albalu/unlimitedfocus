// Thin client for the Butterbase auto-generated REST data API.
// Docs: PostgREST-style filters (column=eq.value), Bearer auth with the
// platform API key (service role — this scraper is a trusted local job).
import { env } from '../env.js';
import { BUTTERBASE_API_BASE } from '../config.js';

async function request(method, apiPath, { params, body } = {}) {
  const url = new URL(`${BUTTERBASE_API_BASE}${apiPath}`);
  for (const [k, v] of Object.entries(params ?? {})) url.searchParams.set(k, v);
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${env('BUTTERBASE_API_KEY')}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`Butterbase ${method} ${apiPath} -> HTTP ${res.status}: ${text.slice(0, 300)}`);
  }
  return res.status === 204 ? null : res.json();
}

export const bb = {
  select: (table, params) => request('GET', `/${table}`, { params }),
  insert: (table, data) => request('POST', `/${table}`, { body: data }),
  update: (table, id, data) => request('PATCH', `/${table}/${id}`, { body: data }),
};

// --- domain helpers -------------------------------------------------------

export async function upsertContact({ platform, handle, displayName = null, profileUrl = null }) {
  // TODO(phase-later): move upsert server-side (Butterbase function) to make it
  // atomic; a single local scraper process doesn't race against itself.
  const found = await bb.select('contacts', {
    platform: `eq.${platform}`,
    handle: `eq.${handle}`,
    limit: '1',
  });
  if (found.length > 0) {
    const c = found[0];
    const patch = { last_seen_at: new Date().toISOString() };
    if (displayName && !c.display_name) patch.display_name = displayName;
    await bb.update('contacts', c.id, patch);
    return c;
  }
  return bb.insert('contacts', {
    platform,
    handle,
    display_name: displayName,
    profile_url: profileUrl,
  });
}

export async function itemExists(platform, externalId) {
  const rows = await bb.select('items', {
    platform: `eq.${platform}`,
    external_id: `eq.${externalId}`,
    select: 'id',
    limit: '1',
  });
  return rows.length > 0;
}

export const insertItem = (item) => bb.insert('items', item);
export const markGraphSynced = (id) => bb.update('items', id, { graph_synced: true });

export const startRun = (platform) => bb.insert('scrape_runs', { platform, status: 'running' });
export const finishRun = (id, { status, stats, error = null }) =>
  bb.update('scrape_runs', id, {
    status,
    stats,
    error,
    finished_at: new Date().toISOString(),
  });

export async function lastCompletedRun(platform) {
  const rows = await bb.select('scrape_runs', {
    platform: `eq.${platform}`,
    status: 'eq.completed',
    order: 'started_at.desc',
    limit: '1',
  });
  return rows[0] ?? null;
}

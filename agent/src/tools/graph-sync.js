#!/usr/bin/env node
// Re-sync items into Neo4j that were captured while the graph was unreachable
// (or during a --no-graph run). Idempotent: MERGE-based, safe to rerun.
// Usage: npm run graph:sync
import { bb, markGraphSynced } from '../lib/butterbase.js';
import { verifyGraph, ensureConstraints, syncItemToGraph, closeGraph } from '../lib/graph.js';

const addr = await verifyGraph();
await ensureConstraints();
console.log(`neo4j connected (${addr})`);

const pending = await bb.select('items', { graph_synced: 'eq.false', order: 'captured_at.asc', limit: '500' });
console.log(`${pending.length} item(s) pending graph sync`);

let synced = 0;
try {
  for (const item of pending) {
    const contact = item.contact_id
      ? (await bb.select('contacts', { id: `eq.${item.contact_id}`, limit: '1' }))[0]
      : { handle: 'unknown', display_name: null };
    await syncItemToGraph({ contact, item, mentions: item.structured?.mentions ?? [] });
    await markGraphSynced(item.id);
    synced++;
  }
} finally {
  await closeGraph();
}
console.log(`synced ${synced}/${pending.length}`);

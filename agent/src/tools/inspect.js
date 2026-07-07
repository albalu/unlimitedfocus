#!/usr/bin/env node
// Quick look at what the scraper has collected so far (reads Butterbase).
// Usage: npm run inspect
import { bb } from '../lib/butterbase.js';

const [runs, contacts, items] = await Promise.all([
  bb.select('scrape_runs', { order: 'started_at.desc', limit: '5' }),
  bb.select('contacts', { order: 'last_seen_at.desc', limit: '100' }),
  bb.select('items', { order: 'captured_at.desc', limit: '15' }),
]);

console.log('── recent runs ─────────────────────────────');
for (const r of runs) {
  console.log(`  ${r.started_at}  ${r.platform}  ${r.status}  ${JSON.stringify(r.stats)}`);
}
if (runs.length === 0) console.log('  (none yet)');

console.log(`\n── contacts (${contacts.length}) ──────────────────────`);
for (const c of contacts.slice(0, 25)) {
  console.log(`  @${c.handle}${c.display_name ? ` (${c.display_name})` : ''}  last seen ${c.last_seen_at}`);
}

console.log(`\n── latest items ────────────────────────────`);
for (const i of items) {
  console.log(`  [${i.kind}] @${(i.url || '').split('/')[3] ?? ''} ${i.topic ?? '—'}  ${i.posted_at ?? i.captured_at}`);
  console.log(`     ${i.brief ?? '(no brief)'}`);
  console.log(`     ${i.url}  graph_synced=${i.graph_synced}`);
}
if (items.length === 0) console.log('  (none yet — run npm run scrape:instagram)');

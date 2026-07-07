#!/usr/bin/env node
// Preflight: verifies every dependency the scraper needs, with exact fix hints.
// Run this once before your first scrape: `npm run check`
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { env } from '../env.js';
import { SCRAPE, BUTTERBASE_APP_ID } from '../config.js';
import { bb } from '../lib/butterbase.js';
import { verifyGraph, closeGraph } from '../lib/graph.js';

const run = promisify(execFile);
let failures = 0;

const ok = (name, detail = '') => console.log(`  ✓ ${name}${detail ? ` — ${detail}` : ''}`);
const bad = (name, detail, hint) => {
  failures++;
  console.log(`  ✗ ${name} — ${detail}${hint ? `\n      fix: ${hint}` : ''}`);
};
const warn = (name, detail, hint) =>
  console.log(`  ~ ${name} — ${detail}${hint ? `\n      fix: ${hint}` : ''}`);

console.log('unlimitedfocus preflight\n');

// 1. env vars (presence only; validity is proven by the live checks below)
for (const name of ['BUTTERBASE_API_KEY', 'NEO4J_USERNAME', 'NEO4J_PASSWORD', 'NEO4J_INSTANCE_NAME', 'ROCKET_RIDE_API_KEY']) {
  try {
    env(name);
    ok(`env ${name}`);
  } catch (e) {
    bad(`env ${name}`, 'missing', `add it to the repo-root .env`);
  }
}

// 2. Butterbase
try {
  await bb.select('contacts', { limit: '1' });
  ok('butterbase', `app ${BUTTERBASE_APP_ID} reachable, schema live`);
} catch (e) {
  bad('butterbase', e.message.slice(0, 200));
}

// 3. Neo4j
try {
  const addr = await verifyGraph();
  ok('neo4j', `connected to ${addr}`);
} catch (e) {
  bad(
    'neo4j',
    e.message.slice(0, 200),
    'if NEO4J_INSTANCE_NAME is not the 8-char Aura instance id, add NEO4J_URI=neo4j+s://<instance-id>.databases.neo4j.io to .env (Aura console → your instance → "Connection URI")'
  );
} finally {
  await closeGraph().catch(() => {});
}

// 4. claude CLI (extraction engine)
try {
  const { stdout } = await run('claude', ['--version'], { timeout: 15_000 });
  ok('claude CLI', stdout.trim());
} catch (e) {
  bad('claude CLI', 'not on PATH', 'install Claude Code (https://claude.com/claude-code)');
}

// 5. Chrome CDP (only needed at scrape time — warn, not fail)
try {
  const res = await fetch(`${SCRAPE.cdpUrl}/json/version`, { signal: AbortSignal.timeout(3000) });
  const info = await res.json();
  ok('chrome CDP', info.Browser);
} catch {
  warn('chrome CDP', `nothing listening at ${SCRAPE.cdpUrl}`, 'run agent/scripts/launch-chrome.sh and log into Instagram in that window');
}

console.log(failures === 0 ? '\nall required checks passed — ready to scrape' : `\n${failures} required check(s) failed`);
process.exit(failures === 0 ? 0 : 1);

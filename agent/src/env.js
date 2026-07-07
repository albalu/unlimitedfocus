// Strict env loading. All secrets live in the repo-root .env (gitignored — this
// repo is public, secrets must never be committed or logged).
// By design there are NO fallbacks: a missing/invalid variable fails loudly
// instead of degrading (owner's requirement).
import { config } from 'dotenv';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const AGENT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const REPO_ROOT = path.resolve(AGENT_ROOT, '..');

config({ path: path.join(REPO_ROOT, '.env'), quiet: true });

export function env(name) {
  const v = process.env[name];
  if (!v) {
    throw new Error(`Missing required env var ${name} — set it in ${path.join(REPO_ROOT, '.env')}`);
  }
  return v;
}

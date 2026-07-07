// Text + image understanding via the local `claude` CLI, so extraction runs on
// the cheap Sonnet tier through the local Claude Code install instead of a
// metered API key.
// TODO(cost): batch several items per invocation once the schema settles.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);

const MODEL = 'sonnet'; // alias -> latest Sonnet; cheap enough for per-item extraction

export async function extractItem({ kind, username, screenshotPath, rawText }) {
  const textBlock = rawText
    ? `\nRaw text scraped alongside it (may include caption, like counts, comments preview):\n---\n${rawText.slice(0, 3000)}\n---`
    : '';
  const prompt = `You are a strict extraction engine for a personal social-media digest.
Read the screenshot at ${screenshotPath}${textBlock}
It is an Instagram ${kind} by @${username}.
Respond with ONLY minified JSON (no markdown fences, no prose) with exactly these keys:
{"media_type":"image|video|carousel|text|unknown","topic":"<1-3 word topic>","ocr_text":"<verbatim text visible in the media, empty string if none>","brief":"<1-2 sentence summary>","detail":"<3-6 sentence detailed description: what is happening, who, where, when if visible>","noteworthy":["<life events worth remembering long-term: birthdays, weddings, moves, travel, launches, achievements — empty array if none>"],"mentions":["<other instagram usernames referenced, without the @>"]}`;

  const { stdout } = await run(
    'claude',
    ['-p', prompt, '--model', MODEL, '--allowedTools', 'Read', '--max-turns', '4'],
    { timeout: 180_000, maxBuffer: 10 * 1024 * 1024 }
  );
  return parseJsonLoose(stdout);
}

// The CLI occasionally wraps output in fences or adds a stray sentence; pull
// out the outermost JSON object. Anything less parseable is a hard error —
// the item is skipped this run and retried next run (dedupe only skips items
// that made it into the DB).
function parseJsonLoose(out) {
  const text = out.trim();
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) {
    throw new Error(`extractor returned no JSON: ${text.slice(0, 200)}`);
  }
  return JSON.parse(text.slice(start, end + 1));
}

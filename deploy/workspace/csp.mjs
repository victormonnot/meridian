// Hash the exact built bytes: Vite may rewrite inline style/script whitespace.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [directory, output] = process.argv.slice(2);
if (!directory || !output) throw new Error('Usage: node csp.mjs DIST OUTPUT');
const hashes = { script: new Set(), style: new Set() };
for (const name of ['index.html', 'workspace.html']) {
  const html = readFileSync(join(directory, name), 'utf8');
  for (const tag of Object.keys(hashes)) {
    const pattern = new RegExp(`<${tag}\\b([^>]*)>([\\s\\S]*?)<\\/${tag}>`, 'gi');
    for (const match of html.matchAll(pattern)) {
      if (tag === 'script' && /\bsrc\s*=/i.test(match[1])) continue;
      hashes[tag].add(`'sha256-${createHash('sha256').update(match[2]).digest('base64')}'`);
    }
  }
}
const policy = [
  "default-src 'none'", `script-src 'self' ${[...hashes.script].join(' ')}`,
  `style-src 'self' ${[...hashes.style].join(' ')}`,
  "script-src-attr 'none'", "style-src-attr 'none'", "connect-src 'self'",
  "font-src 'self'", "img-src 'self' data:", "base-uri 'none'",
  "form-action 'self'", "frame-ancestors 'none'", "object-src 'none'",
].join('; ');
writeFileSync(output, [
  `add_header Content-Security-Policy "${policy}" always;`,
  'add_header X-Content-Type-Options nosniff always;',
  'add_header X-Frame-Options DENY always;',
  'add_header Referrer-Policy same-origin always;',
  'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;',
  'add_header Strict-Transport-Security $meridian_hsts always;',
  '',
].join('\n'));

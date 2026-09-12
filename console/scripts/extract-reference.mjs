// Mechanical extraction of the supplied self-contained HTML, not a UI generator.
// Run only on a reference you own. Never evaluates embedded JavaScript.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import crypto from 'node:crypto';

const html = fs.readFileSync(process.argv[2], 'utf8');
const read = (name) =>
  JSON.parse(html.match(new RegExp(`<script type="__bundler/${name}">([\\s\\S]*?)</script>`))[1]);
const manifest = read('manifest'),
  template = read('template');
const bytes = (id) => {
  const asset = manifest[id];
  if (!asset) throw new Error('Missing embedded resource');
  const data = Buffer.from(asset.data, 'base64');
  return asset.compressed ? zlib.gunzipSync(data) : data;
};
const dest = path.resolve('src/reference');
fs.mkdirSync(dest, { recursive: true });
const publicDir = path.resolve('public/reference');
fs.mkdirSync(publicDir, { recursive: true });
let css = [...template.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
css = css.replace(/url\("([a-f0-9-]{36})"\)/g, (_, id) => {
  const name = `${id}.woff2`;
  fs.writeFileSync(path.join(publicDir, name), bytes(id));
  return `url("/reference/${name}")`;
});
fs.writeFileSync(path.join(dest, 'tokens.css'), css);
for (const icon of read('ext_resources')) {
  if (icon.id.startsWith('icon-'))
    fs.writeFileSync(path.join(publicDir, icon.id.slice(5) + '.svg'), bytes(icon.uuid));
}
const bundle = Object.entries(manifest).find(
  ([, a]) =>
    a.mime.includes('javascript') &&
    bytes(Object.keys(manifest).find((k) => manifest[k] === a))
      .toString()
      .startsWith('/* @ds-bundle:'),
);
if (!bundle) throw new Error('Missing design system');
const source = bytes(bundle[0]).toString();
// Keep authored primitives only. Exclude old product views, mock data and tweak tooling.
const blocks = [
  ...source.matchAll(
    /\/\/ (components\/[^\n]+)\n([\s\S]*?)(?=\n\/\/ (?:components|ui_kits)\/|\n__ds_ns\.Button =)/g,
  ),
];
if (blocks.length < 15) throw new Error('Unexpected reference format');
const code =
  `import React from 'react';\nconst __ds_scope = {};\nconst __ds_ns = {__errors: []};\n` +
  blocks
    .map((m) =>
      m[1].endsWith('/ConfirmModal.jsx')
        ? m[0].replace("position: 'absolute'", "position: 'fixed'")
        : m[0],
    )
    .join('\n')
    .replaceAll('https://unpkg.com/lucide-static@0.544.0/icons/', '/console/reference/')
    .replace(
      ", [['maximize-2', 'Full screen'], ['bell', 'Notifications']].map(([n, l]) => iconBtn(n, l))",
      '',
    ) +
  '\nif (__ds_ns.__errors.length) throw new Error("Reference component initialization failed");\nexport default __ds_scope;\n';
if (/eval\(|new Function|fetch\(/.test(code))
  throw new Error('Unexpected dynamic execution or network code');
fs.writeFileSync(path.join(dest, 'components.js'), code);
fs.writeFileSync(
  path.join(dest, 'provenance.json'),
  JSON.stringify(
    {
      source: 'Ravn Console.html (user supplied)',
      sha256: crypto.createHash('sha256').update(html).digest('hex'),
      components: blocks.map((m) => m[1]),
      note: 'Extracted authored primitives and embedded assets; no HTML bootstrap, Babel, mock data or remote scripts.',
    },
    null,
    2,
  ) + '\n',
);
console.log(`Extracted ${blocks.length} original UI primitives, tokens and local assets.`);

import fs from 'node:fs';
import path from 'node:path';

const [upstreamDir, widget] = process.argv.slice(2);
if (!upstreamDir || !widget) throw new Error('Usage: node scripts/q60r-smoke.mjs <upstream-dir> <wgt>');

const cfg = JSON.parse(fs.readFileSync(new URL('../q60r.config.json', import.meta.url), 'utf8'));
const xml = fs.readFileSync(path.join(upstreamDir, 'app', 'config.xml'), 'utf8');

const checks = [
  ['package id', xml.includes(`package="${cfg.tizen.packageId}"`)],
  ['application id', xml.includes(`id="${cfg.tizen.applicationId}"`)],
  ['service id', xml.includes(`id="${cfg.tizen.serviceId}"`)],
  ['display name', xml.includes(`<name>${cfg.tizen.displayName}</name>`)],
  ['widget renamed', xml.includes('id="https://q60r-adfree.local"')],
  ['widget exists', fs.existsSync(widget)],
  ['widget non-empty', fs.existsSync(widget) && fs.statSync(widget).size > 1024],
];

for (const [name, ok] of checks) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
  if (!ok) process.exitCode = 1;
}

console.log(`Widget: ${widget}`);
console.log(`Size: ${fs.existsSync(widget) ? fs.statSync(widget).size : 0} bytes`);

import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const widget = process.argv[2];
assert(widget, 'pass the generated WGT path');
assert(fs.existsSync(widget), `WGT does not exist: ${widget}`);
assert(fs.statSync(widget).size > 1024, 'WGT is unexpectedly small');

const cfg = JSON.parse(fs.readFileSync('q60r.config.json', 'utf8'));
const xml = execFileSync('unzip', ['-p', widget, 'config.xml'], { encoding: 'utf8' });

assert(xml.includes(`package="${cfg.tizen.packageId}"`));
assert(xml.includes(`id="${cfg.tizen.applicationId}"`));
assert(xml.includes(`id="${cfg.tizen.serviceId}"`));
assert(xml.includes(`required_version="${cfg.tizen.requiredVersion}"`));
assert(xml.includes(`devel.api.version\" value=\"${cfg.tizen.requiredVersion}`));
assert(xml.includes(`<name>${cfg.tizen.displayName}</name>`));
assert(xml.includes('id="https://q60r-adfree.local"'));

assert(!xml.includes('tUb3Xq7Lm9'), 'original package/application/service id leaked into WGT');
assert(!xml.includes('required_version="5.5"'), 'legacy 5.5 required version leaked into WGT');
assert(!xml.includes('devel.api.version\" value=\"5.5'), 'legacy 5.5 API metadata leaked into WGT');

console.log(`WGT regression checks passed: ${widget}`);
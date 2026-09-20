import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const widget = process.argv[2];
assert(widget, 'pass the generated WGT path');
assert(fs.existsSync(widget), `WGT does not exist: ${widget}`);
assert(fs.statSync(widget).size > 1024, 'WGT is unexpectedly small');

const cfg = JSON.parse(fs.readFileSync('q60r.config.json', 'utf8'));
const execZip = (args) => execFileSync('unzip', args, { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });
const readEntry = (name) => execZip(['-p', widget, name]);
const xml = readEntry('config.xml');
const userScript = readEntry('service/dist/assets/userScript.js');
const service = readEntry('service/dist/index.js');

for (const name of [
  'config.xml',
  'index.html',
  'icon.png',
  'service/dist/index.js',
  'service/dist/assets/userScript.js',
  'service/dist/assets/bootScreen.js'
]) {
  const listing = execZip(['-l', widget]);
  assert.match(listing, new RegExp(name.replace(/[.*+?^$(){}|[\]\\]/g, '\\$&')));
}

assert(xml.includes(`package="${cfg.tizen.packageId}"`));
assert(xml.includes(`id="${cfg.tizen.applicationId}"`));
assert(xml.includes(`id="${cfg.tizen.serviceId}"`));
assert(xml.includes(`required_version="${cfg.tizen.requiredVersion}"`));
assert(xml.includes(`devel.api.version\\" value=\\"${cfg.tizen.requiredVersion}`));
assert(xml.includes(`<name>${cfg.tizen.displayName}</name>`));
assert(xml.includes('id="https://q60r-adfree.local"'));
assert(xml.includes('nativeID" value="com.samsung.tv.cobalt-yt"'));
assert(xml.includes('--base_url=http://127.0.0.2:8099/tv'));
assert(xml.includes('<tizen:service id="Q60AdFree1.TubeService" auto-restart="true" on-boot="true">'));

assert(!xml.includes('tUb3Xq7Lm9'), 'original package/application/service id leaked into WGT');
assert(!xml.includes('tUb3Xq7L50'), 'legacy package id leaked into WGT metadata');
assert(!xml.includes('required_version="5.5"'), 'legacy 5.5 required version leaked into WGT');
assert(!xml.includes('devel.api.version\\" value=\\"5.5'), 'legacy 5.5 API metadata leaked into WGT');

for (const token of ['adPlacements', 'adSlots', 'playerAds', 'isInlinePlaybackNoAd', 'SponsorBlock', '_yttv']) {
  assert(userScript.includes(token), `functional userscript token missing from packaged WGT: ${token}`);
}

assert(service.includes('8099'), 'proxy port missing from packaged service');
assert(service.includes('/__tube/boot'), 'boot diagnostics route missing from packaged service');
assert(service.includes('Content-Security-Policy'), 'CSP handling missing from packaged service');

assert(!service.includes('/__tube/dev/'), 'developer routes leaked into packaged service');
assert(!service.includes('x-tube-token'), 'developer auth token leaked into packaged service');

const signatureListing = execZip(['-l', widget]);
assert(!/author-signature\.xml|signature1\.xml/.test(signatureListing), 'unsigned Homebrew WGT unexpectedly contains signature files');

console.log(`WGT regression checks passed: ${widget}`);
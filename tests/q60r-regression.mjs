import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const upstream = process.argv[2];
assert(upstream, 'pass the staged upstream directory');
const cfg = JSON.parse(fs.readFileSync(path.resolve('q60r.config.json'), 'utf8'));

const read = (p) => fs.readFileSync(path.join(upstream, p), 'utf8');

const xml = read('app/config.xml');
assert.match(xml, new RegExp(`package="${cfg.tizen.packageId}"`));
assert.match(xml, new RegExp(`id="${cfg.tizen.applicationId.replace('.', '\\.')}`));
assert.match(xml, new RegExp(`id="${cfg.tizen.serviceId.replace('.', '\\.')}`));
assert.match(xml, new RegExp(`<name>${cfg.tizen.displayName}</name>`));

const runtime = read('framework/runtime/config.js');
assert.match(runtime, /enableAdBlock:\s*true/);
assert.match(runtime, /enableSponsorBlock:\s*true/);
assert.match(runtime, /enableSponsorBlockToasts:\s*false/);

const adblock = read('mods/adblock/index.js');
for (const key of ['adPlacements', 'adSlots', 'playerAds', 'isInlinePlaybackNoAd']) assert.match(adblock, new RegExp(key));

const json = read('framework/registries/json.js');
assert.match(json, /JSON\.parse = function/);
assert.match(json, /JSON\.stringify = function/);
assert.match(json, /window\._yttv/);

console.log('Q60R regression checks passed.');

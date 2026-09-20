import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(process.argv[2] || process.env.UPSTREAM_DIR || '.');
const cfg = JSON.parse(fs.readFileSync(path.resolve(process.cwd(), 'q60r.config.json'), 'utf8'));

const replaceRequired = (file, oldValue, newValue) => {
  const value = fs.readFileSync(file, 'utf8');
  if (!value.includes(oldValue)) throw new Error(`Expected text not found in ${file}: ${oldValue}`);
  fs.writeFileSync(file, value.split(oldValue).join(newValue));
};

const configXml = path.join(ROOT, 'app', 'config.xml');
replaceRequired(configXml, 'tUb3Xq7Lm9', cfg.tizen.packageId);
replaceRequired(configXml, '<name>YouTube</name>', `<name>${cfg.tizen.displayName}</name>`);
replaceRequired(configXml, 'id="https://tube.local"', 'id="https://q60r-adfree.local"');

const packageTool = path.join(ROOT, 'tools', 'package.js');
replaceRequired(packageTool, "packageId: 'tUb3Xq7L50'", `packageId: '${cfg.tizen.packageId}'`);

const runtimeConfig = path.join(ROOT, 'framework', 'runtime', 'config.js');
const runtime = fs.readFileSync(runtimeConfig, 'utf8');
for (const key of cfg.featuresExpectedOn) {
  const rx = new RegExp(`(^|\\n)\\s*${key}:\\s*true\\s*,`, 'm');
  if (!rx.test(runtime)) throw new Error(`Expected default ${key}: true was not found`);
}
for (const key of cfg.featuresExpectedOff) {
  const rx = new RegExp(`(^|\\n)\\s*${key}:\\s*false\\s*,`, 'm');
  if (!rx.test(runtime)) throw new Error(`Expected default ${key}: false was not found`);
}

const adblock = fs.readFileSync(path.join(ROOT, 'mods', 'adblock', 'index.js'), 'utf8');
for (const token of ['adPlacements', 'adSlots', 'playerAds', 'isInlinePlaybackNoAd']) {
  if (!adblock.includes(token)) throw new Error(`AdBlock regression: ${token} missing`);
}

const jsonRegistry = fs.readFileSync(path.join(ROOT, 'framework', 'registries', 'json.js'), 'utf8');
for (const token of ['JSON.parse = function', 'JSON.stringify = function', 'window._yttv']) {
  if (!jsonRegistry.includes(token)) throw new Error(`JSON interception regression: ${token} missing`);
}

console.log(JSON.stringify({
  ok: true,
  model: cfg.target.model,
  requiredVersion: cfg.tizen.requiredVersion,
  packageId: cfg.tizen.packageId,
  applicationId: cfg.tizen.applicationId,
  upstreamCommit: cfg.upstream.commit
}, null, 2));

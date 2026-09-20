import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const source = path.resolve('fixtures/upstream');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'q60r-overlay-'));
fs.cpSync(source, tmp, { recursive: true });
execFileSync(process.execPath, ['scripts/apply-q60r.mjs', tmp], { stdio: 'inherit' });
execFileSync(process.execPath, ['tests/q60r-regression.mjs', tmp], { stdio: 'inherit' });
console.log(`Overlay fixture test passed: ${tmp}`);

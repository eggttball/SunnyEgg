import { build } from 'esbuild';
import { mkdir, copyFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url)), dist = resolve(root, 'dist');
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
for (const file of ['index.html', 'styles.css', 'admin-auth.css']) await copyFile(resolve(root, 'public', file), resolve(dist, file));
await build({ absWorkingDir: root, entryPoints: ['scripts/cloud-entry.js'], outfile: 'dist/app.js', bundle: true, minify: true, target: ['es2020'], format: 'iife', legalComments: 'none' });
console.log('Firebase Hosting 網頁已建立於 dist/。');

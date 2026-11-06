import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { readFile, writeFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { firebaseRuntime } from './firebase-runtime.mjs';
import { prepareDeployment, finalizeDeployment, deploymentEligible } from './firebase-media.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
const snapshotPath = resolve(root, 'dist/deployment-snapshot.private.json');
const receiptPath = resolve(root, 'dist/deployment-success.private.json');
const digest = text => createHash('sha256').update(text).digest('hex');
const args = process.argv.slice(2);
try {
  if (args.some(value => !['--prepare', '--finalize'].includes(value))) throw new Error('Invalid deploy arguments');
  const { repository } = await firebaseRuntime(root, { media: false });
  const mediaRoot = process.env.SUNNYEGG_MEDIA_DIR;
  if (!mediaRoot) throw new Error('Missing media checkout');
  if (args.includes('--prepare')) {
    await rm(receiptPath, { force: true });
    const snapshot = await prepareDeployment({ repository, mediaRoot, dist: resolve(root, 'dist') });
    await writeFile(snapshotPath, JSON.stringify(snapshot), { mode: 0o600 });
    console.log(`Hosting 部署包已備妥：${snapshot.photos.length} 張入選照片。`);
  } else if (args.includes('--finalize')) {
    const text = await readFile(snapshotPath, 'utf8'), receipt = JSON.parse(await readFile(receiptPath, 'utf8'));
    if (receipt.snapshotHash !== digest(text)) throw new Error('Missing successful deployment receipt');
    const snapshot = JSON.parse(text);
    await finalizeDeployment(repository, snapshot);
    console.log('成功部署的照片已同步到公開相簿。');
  } else {
    const snapshotText = await readFile(snapshotPath, 'utf8');
    const snapshot = JSON.parse(snapshotText);
    for (const entry of snapshot.photos) if (!deploymentEligible(await repository.getPhoto(entry.key), entry)) throw new Error('Photo selection changed; prepare a new deployment');
    const deployEnv = { ...process.env }; delete deployEnv.DEBUG;
    for (const key of Object.keys(deployEnv)) if (key.startsWith('FB_')) delete deployEnv[key];
    const child = spawn(process.execPath, [resolve(root, 'node_modules/firebase-tools/lib/bin/firebase.js'), 'deploy', '--project', 'sunnyegg', '--only', 'hosting,firestore', '--non-interactive'], { cwd: root, stdio: 'inherit', env: deployEnv });
    const code = await new Promise((done, reject) => { child.on('error', reject); child.on('exit', value => done(value ?? 1)); });
    if (code !== 0) process.exitCode = code;
    else await writeFile(receiptPath, JSON.stringify({ snapshotHash: digest(snapshotText), deployedAt: Date.now() }), { mode: 0o600 });
  }
} catch { console.error('Firebase 部署尚未完成；請確認服務帳戶、私人照片 checkout 與目前的照片選取狀態。'); process.exitCode = 1; }

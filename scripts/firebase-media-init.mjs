import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GitMediaBucket, MEDIA_REPOSITORY, ensureMediaCheckout } from './firebase-media.mjs';
const execute = promisify(execFile), root = fileURLToPath(new URL('../', import.meta.url));
try {
  try { process.loadEnvFile(resolve(root, '.env')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const destination = process.env.SUNNYEGG_MEDIA_DIR;
  if (!destination || !isAbsolute(destination) || resolve(destination) === resolve(root)) throw new Error('Invalid media directory');
  const privacy = (await execute('gh', ['api', 'repos/' + MEDIA_REPOSITORY, '--jq', '.private'])).stdout.trim();
  if (privacy !== 'true') throw new Error('Media repository must be private');
  await ensureMediaCheckout(destination, () => execute('gh', ['repo', 'clone', MEDIA_REPOSITORY, destination]));
  const bucket = new GitMediaBucket(destination);
  try { await bucket.command(['rev-parse', '--verify', 'HEAD']); }
  catch { await bucket.command(['symbolic-ref', 'HEAD', 'refs/heads/main']); }
  await bucket.verify();
  if (await bucket.command(['status', '--porcelain'])) throw new Error('Media checkout has local changes');
  const workflow = await readFile(resolve(root, '.github/media/notify-sunnyegg.yml'), 'utf8');
  await mkdir(resolve(destination, '.github/workflows'), { recursive: true });
  await writeFile(resolve(destination, '.github/workflows/notify-sunnyegg.yml'), workflow);
  try { await writeFile(resolve(destination, 'README.md'), '# SunnyEggMedia\n\nPrivate selected-photo source for the public SunnyEgg Firebase Hosting gallery. Only selected photos and deployment metadata belong here.\n', { flag: 'wx' }); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  await writeFile(resolve(destination, '.gitignore'), '.env\n.env.*\n*.json.private\n.DS_Store\n');
  await bucket.command(['add', '--', 'README.md', '.gitignore', '.github/workflows/notify-sunnyegg.yml']);
  if (await bucket.command(['diff', '--cached', '--name-only'])) await bucket.command(['commit', '-m', 'Initialize private SunnyEgg media deployment']);
  await bucket.command(['push', '-u', 'origin', 'main']);
  console.log('SunnyEggMedia 已初始化；尚未上傳照片。請依 docs/firebase.md 設定兩個 repo 的 Actions secrets。');
} catch { console.error('SunnyEggMedia 初始化尚未完成。請確認 Private repo、gh 登入、Git 推送權限、乾淨的 main checkout 與 SUNNYEGG_MEDIA_DIR。'); process.exitCode = 1; }

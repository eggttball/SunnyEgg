import { writeFile, appendFile } from 'node:fs/promises';
import { join } from 'node:path';
try {
  const credential = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT_JSON || '');
  if (credential.type !== 'service_account' || credential.project_id !== 'sunnyegg' || !credential.private_key || !credential.client_email) throw new Error();
  if (!process.env.RUNNER_TEMP || !process.env.GITHUB_ENV) throw new Error();
  const path = join(process.env.RUNNER_TEMP, 'sunnyegg-deploy-credentials.json');
  await writeFile(path, JSON.stringify(credential), { mode: 0o600 });
  await appendFile(process.env.GITHUB_ENV, `GOOGLE_APPLICATION_CREDENTIALS=${path}\n`);
} catch { console.error('請設定 GitHub Actions secret FIREBASE_SERVICE_ACCOUNT_SUNNYEGG，內容須為 sunnyegg 專案的部署服務帳戶 JSON。'); process.exitCode = 1; }

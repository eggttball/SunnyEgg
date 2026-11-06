# 免費部署：Firebase Hosting + GitHub 照片 repo

使用 Firebase Spark 的 Hosting、Google Authentication 與 Firestore，不使用 Storage、Cloud Functions 或 Blaze。公開網站上的照片不需要訪客登入；管理員控制只允許 `eggttball@gmail.com` 的已驗證 Google 帳號。

## 資料流

本機 worker 使用原有 Chromium 登入與 Python 模型抓取、辨識照片。只有入選、手動加入且未排除的照片，才會寫入私人 `eggttball/SunnyEggMedia`。GitHub Actions 從私人 repo 取出目前仍入選的檔案，複製到 Firebase Hosting。Firestore 紀錄只在部署成功後公開，因此不會先顯示尚未部署的照片。

私人 repo 是照片的來源儲存位置；Firebase Hosting 上的複本公開供網站訪客查看。參考照片、人臉特徵、未入選照片、Facebook 登入設定檔及私鑰都留在本機，不能放到兩個 repo。

管理員排除照片後，Firestore 立即隱藏該照片。本機 worker 收到排除紀錄後，更新本機負例並從照片 repo 刪除檔案；下次成功部署也會移除 Hosting 的檔案。worker 離線時，刪除檔案會等待它上線；Git 歷史及訪客已下載的複本不會刪除。還原排除後，worker 會重新發布本機保留的原圖。

## Firebase 設定

在 [Firebase Console](https://console.firebase.google.com/project/sunnyegg/overview)：

1. 保持 Spark，啟用 Google 登入。Authentication 授權網域包含 `sunnyegg.web.app`、`sunnyegg.firebaseapp.com`，若用自訂網域也要加入。
2. 建立 Firestore `(default)`。先用 Production mode；專案中的 `firestore.rules` 會在部署時取代預設規則。
3. 專案設定 → 服務帳戶 → 產生私鑰。JSON 放在 Git 專案外；`.env` 只填檔案的絕對路徑。

```dotenv
GOOGLE_APPLICATION_CREDENTIALS=/absolute/private/path/sunnyegg-worker.json
SUNNYEGG_MEDIA_DIR=/absolute/path/SunnyEggMedia
```

服務帳戶適用於 Spark，這些服務不需要 Blaze。前端的 Firebase apiKey 是公開連線設定，不能代替服務帳戶。

worker 服務帳戶需 Firestore 的 Cloud Datastore User 權限；部署服務帳戶需 Firebase Hosting Admin、Firebase Rules Admin、Cloud Datastore User 與 Service Usage Consumer。可使用分開的帳戶，讓部署帳戶沒有本機 Facebook 資料。

## Codex 連接器權限

若讓 Codex 初始化私人 repo，需把 ChatGPT Codex Local Connector 安裝到 GitHub 帳號，並選取 SunnyEgg 與 SunnyEggMedia。只有 OAuth 身分授權、尚未安裝到帳號時，連接器無法存取私人 repo。這個 App 授權與下面的本機 Git/Actions 權杖分開設定。

## 私人照片 repo 初始化

確認 [SunnyEggMedia](https://github.com/eggttball/SunnyEggMedia) 是 **Private**。安裝 GitHub CLI 並 `gh auth login`，Git 也需要有該 repo 的推送權限。執行：

```bash
npm ci
npm run firebase:media-init
```

此指令使用 `.env` 的 `SUNNYEGG_MEDIA_DIR`，只初始化 README、忽略規則與部署通知 workflow，不會上傳照片。若目錄尚未存在，會 clone 指定 repo；若 repo 是空的，會建立 main。後續 worker 每次推送前會檢查 repo 名稱及 Private 狀態，只 stage `photos/` 和入選 manifest。

若不希望在設定完後立即匯入照片，先不要啟動 worker 或 `firebase:sync`。

## GitHub push 自動部署

先建立 [fine-grained token](https://github.com/settings/personal-access-tokens/new)：Resource owner 選 eggttball，Repository access 選 Only select repositories 並加入 SunnyEgg、SunnyEggMedia。Repository permissions 設 Contents → Read-only、Actions → Read and write；Metadata 保持預設。產生後只把 token 放到下列 GitHub Secrets，不要貼到對話。

Secret 名稱與放置位置：

| Repo | Secret 名稱 | 值 |
| --- | --- | --- |
| [SunnyEgg](https://github.com/eggttball/SunnyEgg/settings/secrets/actions) | FIREBASE_SERVICE_ACCOUNT_SUNNYEGG | sunnyegg 服務帳戶 JSON 的完整內容 |
| [SunnyEgg](https://github.com/eggttball/SunnyEgg/settings/secrets/actions) | SUNNYEGG_REPO_TOKEN | 剛產生的 GitHub token |
| [SunnyEggMedia](https://github.com/eggttball/SunnyEggMedia/settings/secrets/actions) | SUNNYEGG_REPO_TOKEN | 同一個 GitHub token |

每一筆都用 New repository secret 新增。服務帳戶 secret 要貼 JSON 內容；本機 .env 的 GOOGLE_APPLICATION_CREDENTIALS 則維持檔案路徑。Codex App 安裝讓 Codex 存取 repo；Actions 需要自己的跨 repo 權杖。

在 **SunnyEgg** repo → Settings → Secrets and variables → Actions 設定：

- `FIREBASE_SERVICE_ACCOUNT_SUNNYEGG`：sunnyegg 部署服務帳戶完整 JSON。
- `SUNNYEGG_REPO_TOKEN`：fine-grained GitHub token，選取 SunnyEgg 與 SunnyEggMedia；Contents **Read**（讀取私人照片），並給 SunnyEgg 的 Actions **Read and write**（觸發部署）。

在 **SunnyEggMedia** repo 設定相同的 `SUNNYEGG_REPO_TOKEN` secret，用來呼叫 SunnyEgg 的部署 workflow。兩個 repo 的 `GITHUB_TOKEN` 無法自行跨 repo 讀取私人照片；權杖只放 GitHub Secrets，不能放前端、manifest 或 repo 檔案。

SunnyEgg push main 會測試、建置、部署 Hosting 與 Firestore rules，再公布成功部署的照片。SunnyEggMedia push main 會通知 SunnyEgg 執行同一個部署 workflow，所有部署共用 SunnyEgg 的 concurrency group，避免不同 repo 同時覆蓋正式網站。可在 SunnyEgg → Actions → Deploy SunnyEgg to Firebase 手動 Run workflow。

## 本機 worker 與照片移轉

保留本機的 `.env`、`data/media`、`data/reference-child`、`data/models`、`data/browser-profile` 及 Python 環境。雲端控制頁不會取得 Facebook 權杖或登入資料。

首次同步目前的入選照片：

```bash
npm run firebase:sync
```

持續接收雲端手動抓取、單篇貼文、指定照片與排除工作：

```bash
npm run firebase:worker
```

worker 必須在使用原始 SunnyEgg 資料的電腦執行。控制頁在 `https://sunnyegg.web.app/?ctrl=1`；登入後可以查看進度與同意排程。本機原有 `npm start` 仍能預覽資料，但雲端作業請使用登入後的控制頁。

## 每日 20:00 先同意

GitHub Actions 於 UTC 12:00（台灣 20:00）建立最近三天的 `awaiting_approval` 工作，即使本機關機也能建立。本機 worker 在線時也會建立同一天的工作；日期 ID 保證不重複。GitHub 的 cron 可能延遲，不能保證精確到分鐘。

管理員登入控制頁後按「同意執行」，工作才進入 queued；worker 上線後開始抓取。未同意的每日工作不會抓取 Facebook，也不妨礙手動工作。每日未同意工作只顯示最新一天，舊工作保留為紀錄。舊的 `python scripts/scheduler.py` 常駐排程已停用，以免繞過同意。

## 本機手動部署

```bash
npm run build
npm run firebase:prepare
npm run firebase:deploy
npm run firebase:finalize
```

只有 deploy 成功後才執行 finalize。prepare 會驗證照片 hash、入選狀態與安全路徑；部署前會再次檢查選取是否變更。如果選取變更，重新執行 prepare、deploy、finalize。

## 免費額度與檢查

Spark 的 Hosting 儲存與傳輸、Firestore 讀寫都有免費額度；到達上限可能暫停服務，不會自動產生 Blaze 費用。私人 repo 的 Actions 也有分鐘額度。這個設計適合少量家庭照片，資料量增加時可壓縮照片、減少部署次數。

`npm test` 檢查原有抓取、辨識、相簿與雲端資料流程。`npm run firebase:test-rules` 以 Java 21+ 的 Firestore Emulator 驗證匿名訪客、其他 Google 帳號、管理員排除/還原、工作送出及每日同意的安全規則。

## Facebook 自動化測試限制

未經使用者明確允許，自動化測試不得實際呼叫 Facebook API、下載 Facebook 媒體或按讚。測試只使用本機 fixture、mock HTTP/browser runner 與 Firebase Emulator。不要啟動正式 worker 或把 daily/scheduler 的 --simulate 當成離線測試；該模式仍可能下載真實照片。

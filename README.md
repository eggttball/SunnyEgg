# SunnyEgg

SunnyEgg is a local photo gallery and processing tool for photos from a Facebook Page. It can discover posts with a dedicated browser profile, download photo attachments through the Graph API, compare them with locally stored reference photos, and show selected photos in a local web page. Video processing is outside the current workflow.

## Run locally

Run all commands from the SunnyEgg project directory. On WSL, use the Ubuntu terminal, not Windows PowerShell. Keep the project in the Linux filesystem (for example, `~/sides/SunnyEgg`).

Requirements: Node.js 20 or later. The Python scripts require Python 3.9 or later; use Python 3.11 or 3.12 for the optional recognition dependencies with their current version constraints.

### Ubuntu / WSL setup

On Ubuntu 26.04, the Ubuntu packages provide Node.js 22:

```bash
sudo apt-get update
sudo apt-get install -y nodejs npm python3-venv curl
node --version
npm --version
python3 --version
```

On other Ubuntu releases, check that `node --version` is at least 20; older distribution packages may need to be replaced with a supported Node.js LTS installation.

When moving from macOS, rebuild dependencies on Linux. `npm ci` replaces the copied `node_modules`. Do not reuse the copied `.venv`: its Python executable and installed native packages belong to macOS. Before creating the new environment, move the old one aside (choose an unused backup path):

```bash
mv .venv /tmp/SunnyEgg-venv-macos-backup
```

### Start the gallery

```bash
npm ci
npm start
```

Open <http://localhost:4173/> in your Windows browser when using WSL 2 with its default localhost forwarding, or <http://127.0.0.1:4173/> on the host. Keep the Ubuntu terminal running; press Ctrl+C to stop the server. The server binds to `127.0.0.1`.

Viewing existing photos only needs the Node.js server and local `data/manifest.json` / `data/media/` files. Chromium, Facebook credentials, Python packages, and face models are needed for fetching or processing photos. Preserve the existing `data/` directory when migrating; a checkout without `data/manifest.json` cannot load the gallery data.

The <http://localhost:4173/?ctrl=1> view shows the local controls for date-range or direct-post fetches and photo feedback. If port 4173 is already in use, start with `PORT=4174 npm start` and open the corresponding URL.

The manual Facebook photo control adds a parent-selected photo without recognition and follows `autoLike` in `config/job.json`. It reports saving and liking separately. If the photo is saved but liking fails, retry the same URL; the saved photo is not duplicated.

### Enable photo fetching and recognition

Install Chromium inside WSL rather than reusing a macOS browser download. On Ubuntu, also install its system dependencies:

```bash
npx playwright install --with-deps chromium
```

On macOS, use `npx playwright install chromium` instead.

Create a fresh Python environment. If your `python3` is version 3.9–3.12:

```bash
python3 -m venv .venv
```

Ubuntu 26.04 ships Python 3.14. The current `numpy<2.1` constraint does not provide Python 3.14 wheels, so use a separate Python 3.12 environment for recognition. One option is uv, which installs a managed Python without replacing the system Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/SunnyEgg-uv-install.sh
sh /tmp/SunnyEgg-uv-install.sh
"$HOME/.local/bin/uv" python install 3.12
"$HOME/.local/bin/uv" venv --python 3.12 --seed .venv
```

Run only one of the environment-creation options above, then install the optional packages:

```bash
.venv/bin/python -m pip install -r requirements-optional.txt
```

Download the YuNet and SFace ONNX model files described in [data/models/README.md](data/models/README.md) into `data/models/`. Put reference photos in `data/reference-child/`. Existing models and reference photos can be retained when migrating; those files stay local and are ignored by Git.

If `.env` is missing, copy `.env.example` to `.env` and set the Facebook API values required for the Page and Meta app you use. Keep an existing `.env` when migrating. Keep tokens and app secrets private.

The browser collector needs its own local sign-in. On WSL, the login command opens a Linux Chromium window through WSLg; use a WSL 2 installation with GUI support. If GUI support is missing, run `wsl --update` from Windows PowerShell and restart WSL before attempting login.

For a profile copied from macOS, close all SunnyEgg browser windows and move it aside before signing in again on Linux (choose an unused backup path):

```bash
mv data/browser-profile data/browser-profile-macos-backup
```

Create or sign in to the Linux profile:

```bash
node scripts/browser-collector.mjs login --profile data/browser-profile
```

After signing in, close all windows of the dedicated browser and wait for the login command to finish. Start or restart the gallery with:

```bash
npm start
```

## Daily processing

The schedule and processing options are in `config/job.json`. The default schedule is 20:00 in the `Asia/Taipei` time zone. To run the scheduler, keep the host available and start:

```bash
.venv/bin/python scripts/scheduler.py --env-file .env
```

The scheduler uses the dedicated browser profile and local model and reference files. It does not run while the host is off. On WSL, it also stops when Windows sleeps or the WSL instance shuts down. `deploy/` contains example systemd files for a Linux host; they are not installed automatically. To use them on WSL, enable systemd and adjust their `/opt/sunnyegg` paths to your project location.

## Local data and privacy

Media, reference photos, recognition feedback, browser sign-in data, downloaded models, run reports, and `.env` are excluded from version control. They are stored under `data/` or in `.env` on the machine running SunnyEgg. Review `.gitignore` before adding any local data to a repository.

## Development

```bash
npm test
```

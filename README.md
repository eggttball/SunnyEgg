# SunnyEgg

SunnyEgg is a local photo gallery and processing tool for photos from a Facebook Page. It can discover posts with a dedicated browser profile, download photo attachments through the Graph API, compare them with locally stored reference photos, and show selected photos in a local web page. Video processing is outside the current workflow.

## Run locally

Requirements: Node.js 20 or later and Python 3.9 or later.

```bash
npm ci
npx playwright install chromium
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-optional.txt
```

Download the YuNet and SFace ONNX model files described in `data/models/README.md` into `data/models/`. Put reference photos in `data/reference-child/`. Those files stay local and are ignored by Git.

Copy `.env.example` to `.env` and set the Facebook API values required for the Page and Meta app you use. Keep tokens and app secrets private. The browser collector also needs its own local sign-in:

```bash
node scripts/browser-collector.mjs login --profile data/browser-profile
```

After signing in, close the dedicated browser window. Start the gallery with:

```bash
npm start
```

Open <http://127.0.0.1:4173/>. The `?ctrl=1` view shows the local controls for date-range or direct-post fetches and photo feedback.

## Daily processing

The schedule and processing options are in `config/job.json`. The default schedule is 20:00 in the `Asia/Taipei` time zone. To run the scheduler, keep the host available and start:

```bash
.venv/bin/python scripts/scheduler.py --env-file .env
```

The scheduler uses the dedicated browser profile and local model and reference files. It does not run while the host is off. `deploy/` contains example systemd files for a Linux host; they are not installed automatically.

## Local data and privacy

Media, reference photos, recognition feedback, browser sign-in data, downloaded models, run reports, and `.env` are excluded from version control. They are stored under `data/` or in `.env` on the machine running SunnyEgg. Review `.gitignore` before adding any local data to a repository.

## Development

```bash
npm test
```

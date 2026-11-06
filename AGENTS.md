# SunnyEgg project instructions

## Facebook verification boundary

Automated tests and verification must use mocked Facebook HTTP responses, fixture media and fake browser runners. Do not call the real Facebook Graph API, download Facebook media, browse Facebook, or send likes as part of automated verification unless the user explicitly authorizes that live test.

`npm test` uses fixtures and mocked Facebook boundaries. `npm run firebase:test-rules` uses the Firestore Emulator. These are the normal verification entry points.

Do not start `firebase:worker`, invoke manual-fetch/manual-photo runners against the real project, or run a daily batch as a test. The legacy scheduler's `--simulate` replays discovery but may still download through the real API; it is not an offline-test mode.

Keep Facebook credentials, reference photos, browser profiles and service-account JSON out of Git commits and tool output.

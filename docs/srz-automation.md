# Srz AutoFlow

Autonomous Multi-Channel Content & Distribution Engine.

## Dashboard design

The dashboard uses a dark operations theme with green action accents, readable
status cards, touch-sized controls, visible keyboard focus and reduced-motion
support. The visual direction was informed by the design-system and UX searches
in [UI UX Pro Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill).
Its recommendations are adapted to the existing Streamlit application; no
React migration, external font requests or third-party UI service is required.
Existing brand/tab icons are retained for continuity.

Theme tokens live in `.streamlit/config.toml`; dashboard styling lives in
`webui/dashboard.css`. The overview refreshes every 30 seconds and displays its
snapshot time. Linked-account counts reflect local credentials, not remotely
verified publishing permissions. The empty Studio includes a guided first-profile
action; no demo accounts, fabricated metrics or automatic publishing are added.
The four-tab bar scrolls horizontally on small screens instead of clipping tabs.

## Four-tab workspace

- **Manual Studio**: select a profile and press **Generate Concept** for a
  one-click private test flight: write, quality-check, narrate and render a preview,
  without uploading. Alternatively **Write Script Only**, edit the narration,
  choose ElevenLabs or English Edge TTS, then **Generate Video**.
  Manual subject, narration, **Video Description / Caption** and comma-separated
  **Visual Keywords** stay editable. Description is publishing metadata, not
  spoken narration; supplied keywords override AI/profile visual terms.
  AI-written titles and cinematic search keywords are passed to the renderer.
- **Auto-Pilot Scheduler**: today's cross-profile timeline, next-trigger countdown,
  independent times, destinations and run statuses. The single **Master Autonomous
  Engine (Active / Standby)** switch persistently controls unattended publishing.
- **Channel Profiles**: add/edit profiles in a dialog, with target audience/tags,
  audience timezone, system prompt, visual keywords, ElevenLabs narrator and
  voice/pacing direction. Each has independent **Daily Posting Schedule (IST)**
  time pickers, YouTube OAuth and masked Meta credentials. Connection cards
  show local configuration; use **Check connections** for live read-only checks.
- **Settings & System Health**: read-only root `.env` inspection; there are no
  global credential inputs or save actions. The board shows service, presence,
  validation result and latency. **Run Health Check** checks configured APIs
  concurrently without sending key values or provider response bodies to the
  browser. Presence initially means configured, not verified connectivity.
  Rotating the file invalidates cached results; run the check again.

**Authorize automated multi-platform dispatch** is the single shared dispatch
authorization switch for **manual Studio dispatch**. Without it, Studio renders
private previews, does not call the legacy Upload-Post fallback, and refuses
direct posting. Independently, activating the master agent authorizes enabled
profile schedules until paused; closing the browser does not pause that worker.
No destinations are implicitly selected.

The social-comment service and legacy backend integrations remain available for
compatibility, but are intentionally absent from this focused dashboard.

## Profiles and niche isolation

Profile metadata lives in `storage/accounts.json`. AI generation binds The
Afterframe to cinematic sci-fi, mystery and psychological thriller stories, and
Mind Vault Paradox to educational dark psychology and Stoic mindset content.
Other profiles use their own niche instructions and visual keywords. Manually
written narration is intentionally editable and is not silently rewritten.
Every AI preparation reads `storage/outputs/<profile_id>/history.json` and
existing exported filenames. Recent stories guide writing; the entire saved
memory guards normalized title and script reuse. Generation retries at most
three times. A separate LLM reviewer evaluates the opening hook and suspenseful
pacing after a deterministic **75-95-word** check. Invalid reviewer JSON or a
failed quality gate blocks that attempt; three failed attempts stop the job.

The opening beat is the first five words, estimating two seconds at 150 WPM,
not measured synthesized-audio timing. LLM judgment is not a guarantee of
audience retention. Voice direction controls narration phrasing/punctuation;
the selected ElevenLabs voice controls synthesis, not a separate instruction API.
Accepted concepts are atomically reserved **before rendering**, so failed renders
and script-only preparations also retain their ideas. Concurrent attempts cannot
reserve the same normalized title/script. Corrupt memory blocks generation rather
than discarding history. Do not delete this file if you want to retain reuse
protection. Filenames seed older titles, but cannot recover unknown old scripts.
This prevents exact normalized reuse, not semantic similarity or all plagiarism.
Manually supplied/edited scripts intentionally bypass AI QA and reservation.

Profiles support stable lowercase slug IDs (for example `the_afterframe`) as
well as existing UUIDs. Existing IDs, tokens and video directories are not renamed.
The canonical schedule field is `post_times_ist`; `post_times` and `slots` remain
synchronized compatibility aliases. `system_prompt` / `niche_prompt` and
`visual_keywords_preset` / `visual_style` are likewise compatible. Profiles
support any distinct minute-of-day times. Top-level `timezone` describes the
audience and handles its daylight-saving display; posting triggers stay in
`Asia/Kolkata`, regardless of the audience timezone.
Example metadata (credential files remain separate):

```json
{
  "profiles": [
    {
      "id": "the_afterframe",
      "name": "The Afterframe",
      "category": "Sci-Fi Thriller & Mystery",
      "target_audience": "United States (US Tier-1)",
      "audience_tags": ["US", "Sci-Fi", "Mystery"],
      "timezone": "America/New_York",
      "system_prompt": "Cinematic micro-film, hook in 2 seconds, twist ending",
      "visual_keywords_preset": "dark neon laboratory, moody corridor",
      "voice_name": "elevenlabs",
      "voice_direction": "Low atmospheric American English, natural pauses",
      "schedule": {"enabled": true, "post_times_ist": ["05:00", "19:30"]},
      "destinations": {"youtube": true, "instagram": false, "facebook": false}
    },
    {
      "id": "mind_vault_paradox",
      "name": "Mind Vault Paradox",
      "category": "Dark Psychology & Stoic Mindset",
      "target_audience": "United States (US Tier-1)",
      "timezone": "America/New_York",
      "system_prompt": "Educational psychology awareness, Stoic reflection",
      "visual_keywords_preset": "suspenseful human behavior, dramatic shadows",
      "schedule": {"enabled": true, "post_times_ist": ["14:00"]},
      "destinations": {"youtube": true, "instagram": true, "facebook": true}
    }
  ]
}
```

Use the profile editor rather than replacing a live accounts file with this example.

New profile renders use:

```text
storage/outputs/<profile_id>/<YYYY-MM-DD>_<subject_slug>.mp4
```

Slugs are filesystem-safe and lowercase; duplicate files receive sequence
suffixes rather than overwriting existing renders. A configured
`app.final_video_export_dir` replaces the output root. Existing videos are not
moved or renamed. Legacy API calls without a profile retain categorized outputs.
Internal task copies remain available for compatibility. Preview/download and
per-account publishers use the exported absolute path.

## Private credentials and connecting channels

### Proposed APInex gateway (local-only preview)

The health tab includes an **inactive** DeepSeek V4 Pro gateway configuration
preview. Optional root `.env` settings are `OPENAI_BASE_URL` (default
`https://api.apinex.bond/v1`) and `OPENAI_MODEL_NAME` (default
`free/deepseek-v4-pro-0813`). These preview settings do not change the current
LLM provider, initialize an SDK client, or redirect scheduled/manual generation.
Only presence of `OPENAI_API_KEY` is displayed; its value is never shown.
The gateway is not contacted by health checks, and no Active badge is claimed.
This local-only scope was explicitly selected instead of enabling network routing.

YouTube credentials are isolated in `storage/tokens/yt_<profile_id>.json`;
Meta secrets are in private `storage/tokens/meta_<profile_id>.json` files.
These are plaintext private service files, not preset exports. Restrict them to
the service identity, keep them off public file servers and encrypt backups.
On Linux use service-owned directories with mode 700 and secret files mode 600.
On Windows apply equivalent NTFS ACLs. Storage and `.env` remain excluded from
version control.

1. In Channel Profiles, add a friendly name, niche, visual keywords and enabled
   destinations. Supply a Meta Page ID, Page token and linked Instagram business
   account ID. A blank token on edit preserves the existing secret.
2. Save, then check connections. Read access does not prove publish permissions.
   Meta requires applicable Page/Instagram scopes, app approval where required,
   and a linked professional Instagram account.
3. Configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` from a Google OAuth
   **Desktop app**, enable YouTube Data API v3 and configure consent/test users.
4. Click Connect YouTube and open the authorization link on the server's local
   desktop. Select the intended channel, then refresh OAuth status. This
   localhost desktop flow is not a remote-server OAuth callback; remote use
   requires a separately registered HTTPS web-client flow.

All listed global credentials are read **directly and exclusively** from the
application-root `.env` (alongside `webui.py` and `config.toml`), on use:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`,
`ELEVENLABS_VOICE_ID`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`, `GOOGLE_CLIENT_ID`,
and `GOOGLE_CLIENT_SECRET`. Inherited process values and old config keys cannot
override or replace these. Missing/empty file values are unconfigured, not
silently substituted. Edit the root `.env` outside the dashboard to rotate keys.
There is no need to restart merely to rotate these keys; in-flight requests
may finish with an already captured key. Other legacy service settings retain
their existing env/config behavior.

Health checks use authenticated read-only model, voice and stock search endpoints;
they never synthesize speech, generate a script, render or upload. Successful read
access does not verify billing balances, generation or publishing permissions.
Google client ID/secret alone cannot prove OAuth access: connect a YouTube
profile first. The check refreshes an existing profile token and reads its channel;
without one it reports **OAuth consent required**, not a false success. API calls
use bounded timeouts and report safe generic errors, never response text.

## Running and scheduling

### Central Master Agent

The scheduler worker now also owns a once-per-minute master supervisor. It
monitors configuration and linked-account readiness, performs read-only API
checks every 15 minutes while active, and reports actionable alerts in
**Auto-Pilot Scheduler > Master Agent · Operations**. In standby it inspects
local configuration only: no paid generation, comment analysis or public replies.
The existing master switch controls both profile schedules and managed replies.
The dashboard also displays a missing-worker heartbeat; the supervisor does not
restart operating-system processes or repair credentials itself.

Policy is persisted in `storage/master_agent_policy.json`. Defaults:

- **One autonomous video reservation per profile per IST day.** The first due slot
  reserves the allowance before paid work; further slots are marked
  `budget_blocked`. Failed/interrupted paid attempts still consume that day's
  reservation. Concurrent due slots and queues inherited from older versions
  are checked again before work. Manual jobs remain separately user-approved.
- **One automatic retry**, five minutes later, only for transient preflight
  `OSError` failures before content, TTS or uploads. File-lock contention retains
  a queued job without paid work. Validation failures need operator action.
  Paid generation/render failures, interrupted work, uncertain uploads and
  partial publishes are never blindly replayed.
- **Up to five attempted replies per profile in a rolling 24 hours**, including
  failures/uncertain attempts; at most one reply per profile per 15-minute poll.
  Paid analysis is limited to 20 recent comments per profile per rolling day.
  Only enabled destinations with saved credentials are monitored. Recent
  comments pass the existing sensitive-content/spam filters and reply validation.
  Comment data is untrusted; profile tokens never enter the analysis prompt.
  Comment requests do not trigger extra videos outside the daily quota.

Reply attempts and analysis reservations are durable in
`storage/master_comment_attempts.json`. A crash after an attempt is recorded
does not cause a second public reply. Uncertain replies and failed analyses
require inspection; they are not silently retried. The old global-token comment
automation defers to this supervisor once its policy exists, including in standby,
so it cannot bypass the master kill switch or create extra videos.

YouTube requires `youtube.force-ssl` in addition to upload/read scopes to reply.
Reconnect older profiles through OAuth to grant that scope. Meta comment APIs
require the relevant approved Page/Instagram permissions; existing upload
permissions alone may not suffice. Platform failures are shown per account
without exposing token strings. Live comment posting is not tested by health
checks. Pausing prevents new actions, but cannot recall an already accepted reply.

Supervisor snapshots live in `storage/master_agent_status.json`; private activity
is recorded in `storage/logs/autoflow_agent.log`. Keep these files private and
intact to preserve limits and at-most-once behavior. Video caps are not a monetary
spend limit: LLM revisions, narration length and comment analysis have provider
costs. No currency budget was configured.

Portable Windows:

```powershell
Set-Location MoneyPrinterTurbo
$env:PYTHONUTF8 = "1"
..\lib\python\python.exe -X faulthandler -m streamlit run webui.py --server.address 127.0.0.1 --server.port 8501
```

Run the FastAPI backend (its lifespan owns the scheduler), or in a separate
terminal run the standalone scheduler, not both:

```powershell
..\lib\python\python.exe -m app.services.scheduler
```

On Linux use the project's virtualenv `python` with the same module arguments.
Keep authentication enabled, bind privately, and use an authenticated HTTPS
reverse proxy before external access.

**The master engine is in standby by default**, including after this upgrade.
Edit each profile's own times, enabled flag and destinations, then activate the
master when ready. All profile times are evaluated in `Asia/Kolkata`; old shared
global cron settings no longer trigger generation, even if their old flag is true.

A single-owner APScheduler worker checks the current IST minute every minute.
It atomically claims each `(profile_id, IST date, time)` in
`storage/autoflow_agent_runs.json` before paid work. Simultaneous accounts queue
independently and render serially rather than skipping one another. A busy
renderer leaves the claim queued for a subsequent tick. Timings are generation
eligibility, not guaranteed upload completion. There is no catch-up for minutes
missed while the worker was offline/paused; already claimed, unstarted work can
resume the same day. Old-day queued work is cancelled.

On restart, running claims become interrupted and are not replayed; queued
claims remain recoverable. Failed/uncertain publishes are never blindly retried.
Pausing the master or disabling/deleting a profile is checked again before
dispatch. It cannot recall an upload already accepted by a platform. Keep
`autoflow_agent_runs.json` intact: deleting it discards duplicate-slot protection.

The staged durable ledger is local SQLite at
`storage/autoflow.sqlite3` (or `app.autoflow_database_path`). It stores slot
claims, job-stage events, final artifact URIs/SHA-256 checksums, topic
reservations, and per-platform publish outbox states. Scheduled render-task
rows link back to their slot-workflow row with `parent_job_id`, so a rendered
task remains traceable to the profile/date/time claim that launched it. The JSON
schedule history and story `history.json` remain compatibility mirrors; legacy
schedule history is imported at worker startup. Queued claims can resume, but
running jobs and posts left `in_flight` after a crash are not replayed
automatically. An `uncertain` or `in_flight` post requires operator review
before any retry.
The additive database migration preserves the existing `jobs`, `artifacts` and
`posts` tables and data. It adds stage status/attempt/input-fingerprint
checkpoints, JSON stage outputs, and checksum-validated artifact records. Script,
terms, narration (when subtitle timing can be rebuilt safely), subtitles,
materials, render, profile-content and publish stages use these checkpoints.
Cached outputs are accepted only when the input fingerprint matches and each
associated artifact still exists with the recorded size and SHA-256. Existing
absolute artifact paths are imported into the portable artifact index if their
files still exist.

Startup recovery marks stale stage work failed and stale in-flight uploads
uncertain; it never repeats paid generation or public uploads automatically.
The daily video limit also consults the durable job ledger, and external
platform IDs cannot be reused across jobs.
The SQLite file and WAL sidecars contain scripts and operational metadata;
protect them as private application data and include them in local backups.
On POSIX, the database and existing sidecars are chmod-restricted to owner-only.
This portable phase does not yet provide Postgres, Temporal, Redis, object
storage, KMS, distributed workers, or a review/repair console for uncertain
posts.

The autonomous renderer retains portrait 1080x1920 output, subtitles, ElevenLabs
narration and profile-specific stock search. The existing FFmpeg assembly is
unchanged. Private per-stage events, quality result, subject, task ID and individual
platform results are appended to `storage/logs/autoflow_agent.log`.
History is also stored in `storage/dispatch_history.json`, the durable agent
ledger and scheduler/task records. Never publish these private files.

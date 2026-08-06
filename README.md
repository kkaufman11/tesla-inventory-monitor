# Tesla Inventory Monitor — Model Y Performance RWD near 94402

Polls Tesla's public inventory search (the same endpoint tesla.com/inventory
uses in your browser — no login/API key needed) for vehicles matching a
model + trim filter within a radius of your ZIP code, and texts you the
first time a new matching VIN appears.

**Two ways to run this — pick one:**
- **Option A: GitHub Actions (recommended)** — runs on GitHub's servers every
  5 minutes, forever, with nothing of yours needing to stay on. Set up once,
  never touch it again. See "Option A" below.
- **Option B: your own machine via cron** — simpler to debug, but only runs
  while your computer is on. See "Option B" further down.

**Heads up on the search itself:** as of today, Tesla's Model Y Performance
trim is AWD-only — a Performance *RWD* configuration hasn't been released
(it's been spotted testing and is expected later this year). So out of the
box this is configured to alert on any Model Y **Performance** trim
regardless of drivetrain, so you're not waiting on a config that can never
match. Flip `REQUIRE_RWD=true` in `.env` once Tesla actually ships it, or
just watch the SMS text (it includes drivetrain) and ignore AWD hits for now.

## Set up Twilio (for SMS) — needed either way

1. Create a free trial account at https://www.twilio.com/try-twilio
2. From the [console](https://console.twilio.com), copy your **Account SID**
   and **Auth Token**.
3. Get a Twilio phone number (trial accounts get one free) — that's your
   `TWILIO_FROM_NUMBER`.
4. Your own cell number, in `+1XXXXXXXXXX` format, is `TWILIO_TO_NUMBER`.
5. Trial accounts can only text numbers you've **verified** in the Twilio
   console (Phone Numbers → Verified Caller IDs) — verify your own number
   there, or upgrade the account (a few dollars) to remove that restriction.

---

## Option A: GitHub Actions (runs forever, nothing of yours stays on)

1. **Create a new GitHub repo** (private is fine — free) and push this
   folder to it:
   ```bash
   cd tesla-inventory-monitor
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/tesla-inventory-monitor.git
   git push -u origin main
   ```
   (`.env` is git-ignored on purpose — your Twilio credentials never get
   committed. Real values go into GitHub's encrypted Secrets, next step.)

2. **Add secrets**: repo → Settings → Secrets and variables → Actions →
   *Secrets* tab → New repository secret. Add these four:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM_NUMBER`
   - `TWILIO_TO_NUMBER`

3. **Add variables** (same page, *Variables* tab — these aren't secret, just
   your search settings):
   - `ZIP` = `94402`
   - `RANGE_MILES` = `200`
   - `MODEL` = `y`
   - `CONDITION` = `new`
   - `TRIM_KEYWORDS` = `performance`
   - `REQUIRE_RWD` = `false`

4. **Enable the workflow**: repo → Actions tab → you should see "Tesla
   Inventory Monitor" listed → click "Enable workflow" if prompted.

5. **Test it manually**: Actions tab → "Tesla Inventory Monitor" →
   "Run workflow" button → Run. Click into the run and check the logs —
   confirm it found results and didn't error. If Twilio is fully configured
   and there's a live match, you'll get a text within a minute or two.

That's it — `.github/workflows/monitor.yml` is already set to run every 5
minutes on its own from here on. Each run commits the updated list of
already-seen VINs back to the repo (that's the `git push` step in the
workflow) so you never get double-texted about the same vehicle. You can
watch history and adjust variables any time from the repo's Actions/Settings
tabs — no code changes needed for basic tweaks like ZIP or radius.

**A note on reliability:** GitHub's `schedule` trigger is "best effort" —
during periods of high platform load it can run a few minutes late, and
GitHub disables scheduled workflows automatically after **60 days of repo
inactivity** (any commit, including the bot's own state-file commits,
resets that clock — so in practice it won't go dormant on its own, but it's
worth knowing).

---

## Option B: your own machine via cron

Only runs while your computer is on and awake — use this if you'd rather
not put anything in the cloud, or want to debug locally first.

```bash
cd tesla-inventory-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Twilio values and search settings
```

Test it:

```bash
source venv/bin/activate
python3 tesla_monitor.py --debug
```

This runs one check, logs what it found to `monitor.log`, and writes the
full raw API response to `last_raw_response.json` so you can sanity-check
what Tesla is actually returning for your area. Run it twice in a row — the
second run should say "No new matching vehicles" since matches are deduped
by VIN in `seen_vehicles.json`. Delete that file if you ever want to
re-alert on everything currently in inventory.

Then schedule it:

```bash
crontab -e
```

Add a line to run every 5 minutes (use the **full absolute path**, since
cron doesn't inherit your shell's PATH or activated venv):

```
*/5 * * * * cd /full/path/to/tesla-inventory-monitor && venv/bin/python3 tesla_monitor.py >> cron.log 2>&1
```

Logs go to both `monitor.log` (from the script) and `cron.log` (cron's own
stdout/stderr capture) — check `cron.log` first if it seems like cron isn't
running at all.

Either option: keep the interval reasonable (5 min is plenty) — hitting the
endpoint too aggressively risks Tesla rate-limiting or blocking the
requesting IP.

## Files

| File | Purpose |
|---|---|
| `tesla_monitor.py` | Main script — run manually or via cron |
| `.env` | Your config (ZIP, filters, Twilio creds) — not committed anywhere |
| `seen_vehicles.json` | VINs already alerted on, auto-maintained |
| `monitor.log` | Human-readable run history |
| `last_raw_response.json` | Only written in `--debug` mode |

## Tuning the filter

`TRIM_KEYWORDS` in `.env` is a comma-separated, case-insensitive list —
the vehicle record must contain **at least one** of these strings anywhere
in Tesla's JSON for it to match. Since Tesla's internal trim/option codes
shift between refreshes, matching on the human-readable text is more
robust than hardcoding a trim code that might silently stop working.

If you want to see exactly what fields Tesla returns for a given vehicle
(to build a tighter filter), run with `--debug` and inspect
`last_raw_response.json`.

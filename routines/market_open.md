# Amara — Market Open Run (9:35 AM ET)

## Schedule
Cron: `35 13 * * 1-5`  (13:35 UTC = 9:35 AM ET, weekdays only)

## Environment
All API keys are injected as environment variables by the Cloud Environment.
Do NOT look for a secrets.env file — it is not in this repo.

Required variables:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ANTHROPIC_API_KEY`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_USER_IDS`

## Your job

1. Install dependencies if not already present:
   ```
   pip install alpaca-py anthropic pandas ta requests python-dotenv pytz --quiet
   ```

2. Run Amara:
   ```
   python amara.py
   ```

3. Watch for errors. If the run fails, log the error clearly and stop — do not retry.

4. After a successful run, commit any changed files back to the main branch so the next routine picks up the latest state:
   ```
   git add amara_dashboard.md amara_trades.json amara.log
   git commit -m "Amara market-open run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
   git push origin main
   ```

   If there are no changes (market closed, no trades), still commit to confirm the run happened:
   ```
   git commit --allow-empty -m "Amara market-open run $(date -u +%Y-%m-%dT%H:%M:%SZ) — no changes"
   git push origin main
   ```

## Notes
- Amara is a single-run serverless bot. It runs once and exits cleanly.
- It reads `amara_dashboard.md` for prior-run context before scanning.
- It writes `amara_dashboard.md` and `amara_trades.json` after each run.
- LINE notifications are suppressed before 03:45 AM Taiwan time (only the pre-close run fires them).
- Do not modify any trading logic — just run the script and commit the outputs.

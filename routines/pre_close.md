# Amara — Pre-Close Run (3:50 PM ET)

## Schedule
Cron: `50 19 * * 1-5`  (19:50 UTC = 3:50 PM ET, weekdays only)

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

4. After a successful run, commit any changed files back to the main branch:
   ```
   git add amara_dashboard.md amara_trades.json amara.log
   git commit -m "Amara pre-close run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
   git push origin main
   ```

   If there are no changes:
   ```
   git commit --allow-empty -m "Amara pre-close run $(date -u +%Y-%m-%dT%H:%M:%SZ) — no changes"
   git push origin main
   ```

## Notes
- This is the session-end run. Amara will check trailing stops, review open positions, and send the LINE summary message (gate: 03:45 AM Taiwan time — this run fires at ~03:50 AM TWN, so LINE fires).
- Amara reads `amara_dashboard.md` for prior-run context before acting.
- It writes updated `amara_dashboard.md` and `amara_trades.json` after the run.
- Do not modify any trading logic — just run the script and commit the outputs.

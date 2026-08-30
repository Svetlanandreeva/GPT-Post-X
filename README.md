# Svetlana Social Publisher

Free-ish publishing pipeline for **X + Threads** using GitHub Actions + Buffer.

## Safety first

The repository starts in safe mode:

- `config.json` has `"dry_run": true`
- the example post has `"enabled": false`
- nothing will be published until both are deliberately changed

## How it works

1. Approved posts live in `posts/posts.json`.
2. GitHub Actions runs once a day.
3. The script finds the connected X (`twitter`) and Threads channels in Buffer automatically.
4. It sends only posts scheduled within the next few days to Buffer.
5. Buffer publishes them at the exact `publish_at` UTC time.
6. Existing matching scheduled posts are skipped to reduce duplicate publishing.

## One-time setup

1. Create a Buffer account.
2. Connect your X account and Threads account in Buffer.
3. In Buffer open **Settings → API** and create an API key.
4. Create a private GitHub repository and upload/push this project.
5. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
6. Name it `BUFFER_API_KEY` and paste the Buffer key.
7. Run the GitHub Action manually once while `dry_run` is still `true`.

Never commit the API key into a file.

## Add a post

Edit `posts/posts.json`:

```json
{
  "id": "2026-09-01-opinion-01",
  "enabled": true,
  "publish_at": "2026-09-01T15:00:00Z",
  "x": "Short X version.",
  "threads": "Slightly more conversational Threads version."
}
```

`publish_at` should be ISO-8601 with timezone. Using `Z` (UTC) is simplest.

You can omit either `x` or `threads` for a post that should go to only one platform.

## Turn publishing on

After the dry run is successful, change in `config.json`:

```json
"dry_run": false
```

Commit the change. On the next Action run, enabled posts within the sync horizon will be scheduled in Buffer.

## Free-plan friendly behavior

`sync_horizon_days` defaults to 5 so the system does not try to fill a huge queue. If you post twice a day and hit Buffer's queue limit, reduce it to 3–4 days.

## Content workflow

Recommended workflow:

- We prepare the monthly plan and copy together.
- Only approved copy gets `enabled: true`.
- Automation handles posting.
- Human time stays focused on replies, likes and conversations.

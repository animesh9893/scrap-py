# LinkedIn hiring-alert posts

Moved here from the old job-scraper repo (`src/linkedin-posts.js`, `src/post-image.js`), now reading the scrap-py
Supabase project. Node, separate from the Python scraper: `npm ci` and `npm test` in this folder.

1. **Write posts** (`.github/workflows/posts.yml`, every hour in proj-berry/scrap-py): the best new jobs (view
   `post_candidates`) are split between the people in `config.js`, so each gets a different set, and one post is
   written for each, at most once every `everyHours` (4) per person and only when they'd get `minJobs` (10) jobs.
   Each job goes into one post only, ever. The text links every job to its page on https://krawlr.xyz and ends
   with the home page; hashtags come from `config.js`, the jobs' categories, cities and companies.
2. **Picture** (`post-image.js`): a 1080×1350 PNG with the opening count, a card per company (up to 6) with its
   LinkedIn logo, roles and places, a strip of premium companies (`companies.is_premium`) and the site at the
   bottom. Stored in `posts.image`, served by the view `public_post_images`. Fonts: Inter (OFL) in `fonts/`.
3. **n8n** (each person's own, `n8n/hiring-alert-workflow.json`): fetches the person's newest draft from
   `public_linkedin_posts`, checks it against the template, sends the picture and text to Telegram with
   Approve/Decline, posts to LinkedIn on Approve, then reports `posted` or `skipped` to
   `https://krawlr.xyz/api/post-status` (Netlify function) with the person's token. A person with an
   `N8N_WEBHOOK_<ID>` secret gets pinged right after their post is written; without one, n8n's own schedule picks
   the draft up.

The private page `/posts.html` lists every post and its status.

## Setup per person (`<ID>` = their id in capitals, e.g. `ANIMESH`)

- A token: `openssl rand -hex 24`. Netlify env `POST_TOKEN_<ID>` (functions scope), the same value in the n8n
  Settings node `token`, and, only when using the webhook, the GitHub secret `POST_TOKEN_<ID>`.
- n8n: import `n8n/hiring-alert-workflow.json`, fill Settings (`person`, `telegramChatId`, `token`; `siteUrl`,
  `supabaseUrl` and `supabaseKey` are already the krawlr.xyz and scrap-py values), pick the Telegram and LinkedIn
  credentials in their nodes, activate.
- Netlify also needs `SUPABASE_URL` and `SUPABASE_SECRET_KEY` for `/api/post-status`.

Keep `checkPost()` in `linkedin-posts.js` and the workflow's "Check against template" node the same; a test checks.

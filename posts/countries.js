// Every hour (.github/workflows/country-posts.yml): LinkedIn posts for readers outside India, one country per account
// in countries-config.js. Same template, checks and picture as the India posts (linkedin-posts.js, post-image.js),
// whose code this only imports: the India posts and their workflow are left as they are. Each post names jobs found
// by a search of one of the country's locations (never remote ones or ones in India, which the India posts take, so
// the two never pick the same job), says how many jobs are live there and links to the board filtered to it.
// Each account's n8n (n8n/country-alert-workflow.json) starts this workflow for its account, then posts the draft.
// Usage: node posts/countries.js   (needs DATABASE_URL; ACCOUNT=usa writes only that account's post)
import { pathToFileURL } from 'node:url';
import { buildPost, checkPost, payFor, splitJobs } from './linkedin-posts.js';

const MAX_CHARS = 3000;

/** The board filtered to a country's locations: https://krawlr.xyz/?places=United%20States,Seattle */
export function boardUrl(siteUrl, places) {
    const site = `${siteUrl.replace(/\/$/, '')}/`;
    return places.length ? `${site}?places=${places.map(encodeURIComponent).join(',')}` : site;
}

/**
 * India's post made for one country: its flag on roles without one (a country linkedin-posts.js doesn't know),
 * the country's word for entry level jobs instead of #freshers, and the board line linking to the country's jobs.
 */
export function localize(body, { siteUrl, flag, entryTag, places = [] }) {
    const site = `${siteUrl.replace(/\/$/, '')}/`;
    return body.split('\n').map((line) => {
        if (flag && line.startsWith('🌍 ')) return `${flag} ${line.slice('🌍 '.length)}`;
        if (line.startsWith('🔎 ') && line.endsWith(` ${site}`)) return `${line.slice(0, -site.length)}${boardUrl(siteUrl, places)}`;
        if (line.startsWith('#') && entryTag) return line.replace(/#freshers\b/, `#${entryTag}`);
        return line;
    }).join('\n');
}

// "Seattle, Washington, United States" → "Seattle"; as linkedin-posts.js says places, for the picture.
function where({ location, workplace }) {
    const city = String(location ?? '').split(',')[0].trim().replace(/\s+division$/i, '');
    if (workplace === 'remote') return /remote/i.test(city) || !city ? 'Remote' : `${city} (Remote)`;
    return city;
}

// The picture of each new post, as linkedin-posts.js draws them (its drawImages, only for these posts).
async function drawImages(store, siteUrl, ids) {
    if (!ids.length) return;
    const { renderPostImage, loadLogos, pickCompanies, MAX_FEATURED, MAX_STRIP } = await import('./post-image.js');
    const { rows } = await store.query(
        `SELECT p.id, p.created_at, COALESCE(c.slug, j.company) AS slug, COALESCE(c.name, j.company) AS name,
                c.logo_url, c.domain, COALESCE(c.is_premium, false) AS is_premium, j.title, j.location, j.workplace
         FROM posts p
         JOIN post_jobs pj ON pj.post_id = p.id
         JOIN jobs j ON j.source = pj.source AND j.job_id = pj.job_id
         LEFT JOIN companies c ON c.slug = j.company_slug
         WHERE p.id = ANY($1) AND p.image IS NULL
         ORDER BY p.id, pj.position`, [ids]);
    const { rows: premium } = await store.query(
        `SELECT c.slug, c.name, c.logo_url, c.domain, true AS is_premium
         FROM companies c LEFT JOIN company_index ci ON ci.slug = c.slug
         WHERE c.is_premium ORDER BY ci.score DESC NULLS LAST, c.sort_order, c.name LIMIT $1`, [MAX_STRIP + MAX_FEATURED]);
    const logos = new Map();
    const withLogo = (c) => ({ ...c, logo: logos.get(c.slug)?.logo ?? null });
    for (const id of new Set(rows.map((r) => r.id))) {
        const mine = rows.filter((r) => r.id === id);
        const postCompanies = new Map();
        for (const r of mine) {
            if (!postCompanies.has(r.slug)) postCompanies.set(r.slug, { ...r, jobs: [] });
            postCompanies.get(r.slug).jobs.push({ title: r.title, place: where(r) });
        }
        const { featured, more, strip } = pickCompanies([...postCompanies.values()], premium);
        for (const c of await loadLogos([...featured, ...strip].filter((c) => !logos.has(c.slug)))) logos.set(c.slug, c);
        const png = renderPostImage({ featured: featured.map(withLogo), more, strip: strip.map(withLogo), jobs: mine.length,
            companiesInPost: postCompanies.size, siteUrl, date: mine[0].created_at });
        // Only if India's hourly run hasn't drawn it in the meantime (it draws every draft without one).
        await store.query('UPDATE posts SET image = $2 WHERE id = $1 AND image IS NULL', [id, png]);
        console.log(`  🖼 post ${id}: picture with ${featured.length} cards and ${strip.length} more logos`);
    }
}

async function main() {
    const { countryPosts: cfg } = await import('./countries-config.js');
    const { default: pg } = await import('pg');
    if (!process.env.DATABASE_URL) throw new Error('Missing env var DATABASE_URL (a GitHub Actions secret)');
    const only = (process.env.ACCOUNT ?? '').trim().toLowerCase();
    const all = cfg.countries.flatMap((c) => c.accounts.map((a) => a.id));
    if (only && !all.includes(only)) throw new Error(`No account "${only}" in countries-config.js (${all.join(', ')})`);
    const store = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2,
        ssl: process.env.DATABASE_SSL === 'false' ? false : { rejectUnauthorized: false } });
    const made = [];
    try {
        const { rows: top } = await store.query(
            `SELECT c.hashtag FROM companies c JOIN company_index ci ON ci.slug = c.slug
             WHERE c.premium_tier = 1 AND c.hashtag IS NOT NULL ORDER BY ci.jobs_week DESC, c.name LIMIT 8`);
        // One country at a time: its posts are saved before the next country picks, so a job found by searches in
        // two countries goes into one post only.
        for (const country of cfg.countries) {
            const accounts = country.accounts.filter((a) => !only || a.id === only);
            if (!accounts.length) continue;
            // As the India posts pick (linkedin-posts.js), but only this country's jobs: found by a search of one of
            // its locations, not remote, not in India.
            const { rows: candidates } = await store.query(
                `SELECT pc.*, COALESCE(pc.role, CASE j.category WHEN 'ai' THEN 'ml' ELSE j.category END) AS role,
                        j.seniority, c.hashtag AS company_tag, $1::text AS country
                 FROM post_candidates pc JOIN jobs j USING (source, job_id) LEFT JOIN companies c ON c.slug = pc.company_slug
                 WHERE pc.workplace IS DISTINCT FROM 'remote' AND COALESCE(pc.location, '') NOT ILIKE '%india%'
                   AND EXISTS (SELECT 1 FROM job_search_hits h JOIN locations l ON l.id = h.location_id
                               WHERE h.source = j.source AND h.job_id = j.job_id AND l.country = $1)
                   AND NOT EXISTS (SELECT 1 FROM job_search_hits h JOIN locations l ON l.id = h.location_id
                                   WHERE h.source = j.source AND h.job_id = j.job_id AND l.country = 'IN')
                 ORDER BY pc.first_seen_at > now() - interval '6 hours' DESC, pc.nth_at_company, pc.score DESC, pc.first_seen_at DESC
                 LIMIT $2`, [country.code, cfg.jobsPerPost * accounts.length]);
            const { rows: salaries } = await store.query(
                `SELECT company_slug, query, currency, median_annual::float AS median FROM company_salaries
                 WHERE company_slug = ANY($1) ORDER BY median_annual DESC`, [[...new Set(candidates.map((j) => j.company_slug))]]);
            for (const job of candidates) job.pay = payFor(job, salaries);
            const { rows: [{ live }] } = await store.query(
                `SELECT COUNT(DISTINCT (h.source, h.job_id))::int AS live FROM job_search_hits h
                 JOIN locations l ON l.id = h.location_id JOIN jobs j ON j.source = h.source AND j.job_id = h.job_id
                 WHERE l.country = $1 AND j.status <> 'skipped' AND j.closed_at IS NULL`, [country.code]);
            const { rows: places } = await store.query(
                `SELECT name FROM locations WHERE country = $1 AND is_active ORDER BY priority, name`, [country.code]);
            const options = { board: { live, countries: [country.code] }, siteUrl: cfg.siteUrl, hashtags: country.hashtags,
                maxHashtags: cfg.maxHashtags, boardTags: top.map((r) => r.hashtag), scrapeEvery: cfg.scrapeEvery, followAsk: cfg.followAsk };
            const write = (jobs) => {
                const body = localize(buildPost({ jobs, ...options }), { siteUrl: cfg.siteUrl, flag: country.flag,
                    entryTag: country.entryTag, places: places.map((p) => p.name) });
                // linkedin-posts.js names only IN, US and CA; any other country would read "across several countries".
                return body.replace('live across several countries.', `live across ${country.name}.`);
            };
            const split = splitJobs(candidates, accounts, cfg.jobsPerPost);
            for (const account of accounts) {
                let jobs = split.get(account.id);
                const { rows: [last] } = await store.query(
                    `SELECT EXTRACT(EPOCH FROM now() - max(created_at)) / 3600 AS hours FROM posts WHERE person = $1`, [account.id]);
                if (process.env.FORCE_POSTS !== 'yes' && last?.hours != null && last.hours < cfg.everyHours - 0.5) {
                    console.log(`  ${account.id}: last post ${Number(last.hours).toFixed(1)}h ago, next after ${cfg.everyHours}h`);
                    continue;
                }
                if (jobs.length < cfg.minJobs) {
                    console.log(`  ${account.id}: ${jobs.length} new job(s) in ${country.code}, fewer than ${cfg.minJobs}, no post`);
                    continue;
                }
                let body = write(jobs);
                while (body.length > MAX_CHARS && jobs.length > cfg.minJobs) {
                    jobs = jobs.slice(0, -1);
                    body = write(jobs);
                }
                const tx = await store.connect();
                let id;
                try {
                    await tx.query('BEGIN');
                    ({ rows: [{ id }] } = await tx.query('INSERT INTO posts (person, body) VALUES ($1, $2) RETURNING id',
                        [account.id, body]));
                    for (const [i, j] of jobs.entries()) {
                        await tx.query('INSERT INTO post_jobs (post_id, source, job_id, position) VALUES ($1, $2, $3, $4)',
                            [id, j.source, j.job_id, i + 1]);
                    }
                    await tx.query('COMMIT');
                } catch (err) {
                    await tx.query('ROLLBACK').catch(() => {});
                    throw err;
                } finally {
                    tx.release();
                }
                const problems = checkPost(body);
                if (problems.length) console.warn(`  ! post ${id} doesn't match the template: ${problems.join('; ')}`);
                made.push(Number(id));
                console.log(`  ✔ ${account.id}: post ${id} with ${jobs.length} ${country.code} job(s)`);
            }
        }
        await drawImages(store, cfg.siteUrl, made);
    } finally {
        await store.end();
    }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
    await main();
}

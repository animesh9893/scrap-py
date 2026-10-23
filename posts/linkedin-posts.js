// Every hour (.github/workflows/posts.yml): splits the best new jobs (view post_candidates) between the people in
// config.js, writes one LinkedIn post with its picture and job links for each (table posts, shown on the private
// page /posts.html), then pings each person's n8n when they have a webhook (secret N8N_WEBHOOK_<ID>). Without one, their
// n8n picks up the newest draft on its own schedule and asks for approval on Telegram.
// Usage: node posts/linkedin-posts.js   (needs DATABASE_URL)
import { pathToFileURL } from 'node:url';
import { payRoles } from '../web/pay-roles.js';

export { payRoles };

const MAX_CHARS = 3000;   // LinkedIn's limit for a post

/** Deals jobs (best first) out in turn, so each person gets an equally good, different set. */
export function splitJobs(jobs, people, perPost) {
    const out = new Map(people.map((p) => [p.id, []]));
    let i = 0;
    for (const job of jobs) {
        const open = people.filter((p) => out.get(p.id).length < perPost);
        if (!open.length) break;
        out.get(open[i++ % open.length].id).push(job);
    }
    return out;
}

// LinkedIn posts have no bold or italic, so, like LinkedIn formatters, we use Unicode's sans-serif bold and
// italic letters (𝗔 = A + 0x1D5D4 − 0x41). Other characters are kept as they are.
const STYLES = { bold: { A: 0x1d5d4, a: 0x1d5ee, 0: 0x1d7ec }, italic: { A: 0x1d608, a: 0x1d622 } };
function styled(text, style) {
    return String(text).replace(/[A-Za-z0-9]/g, (c) => {
        const first = /[A-Z]/.test(c) ? 'A' : /[a-z]/.test(c) ? 'a' : '0';
        const start = STYLES[style][first];
        return start ? String.fromCodePoint(start + c.charCodeAt(0) - first.charCodeAt(0)) : c;
    });
}
export const bold = (text) => styled(text, 'bold');
export const italic = (text) => styled(text, 'italic');

/** Turns bold and italic letters back into plain ones, so checks and searches see ordinary text. */
export function plain(text) {
    return [...String(text ?? '')].map((ch) => {
        const cp = ch.codePointAt(0);
        for (const [start, first, count] of [[0x1d5d4, 65, 26], [0x1d5ee, 97, 26], [0x1d7ec, 48, 10], [0x1d608, 65, 26], [0x1d622, 97, 26]]) {
            if (cp >= start && cp < start + count) return String.fromCharCode(first + cp - start);
        }
        return ch;
    }).join('');
}

// "Bengaluru, Karnataka, India" → "Bengaluru"; remote jobs say so.
function where({ location, workplace }) {
    const city = String(location ?? '').split(',')[0].trim();
    if (workplace === 'remote') return /remote/i.test(city) || !city ? 'Remote' : `${city} (Remote)`;
    return city;
}

// "Node.js" → "nodejs", "C++" → "cpp", "C#" → "csharp", "Machine Learning" → "machinelearning".
const hashtag = (tag) => tag.toLowerCase().replace(/\+\+/g, 'pp').replace(/#/g, 'sharp').replace(/[^a-z0-9]/g, '');

// Hashtags people follow for each role (jobs.role).
const ROLE_TAGS = {
    backend: 'backenddeveloper', frontend: 'frontenddeveloper', fullstack: 'fullstackdeveloper', mobile: 'mobiledevelopment',
    data: 'dataengineering', ml: 'machinelearning', devops: 'devops', security: 'cybersecurity', qa: 'softwaretesting',
    design: 'uxdesign', product: 'productmanagement', manager: 'engineeringmanagement',
};

// The name people tag a city by: LinkedIn's "Greater Kolkata Area" → kolkata, Bengaluru → bangalore.
const CITY_TAGS = { bengaluru: 'bangalore', gurugram: 'gurgaon', newdelhi: 'delhi', navimumbai: 'mumbai', newyorkcity: 'newyork',
    sanfranciscobayarea: 'sanfrancisco', bombay: 'mumbai', madras: 'chennai' };
function cityTag(city) {
    const name = hashtag(String(city).replace(/^greater\s+/i, '').replace(/\s+(metropolitan\s+)?(area|region)$/i, ''));
    return name ? `${CITY_TAGS[name] ?? name}jobs` : null;
}

// The countries the board searches (locations.country), as the flag in front of a role, how the post names them and
// the hashtag people follow.
const COUNTRIES = {
    IN: { flag: '🇮🇳', name: 'India', tag: 'indiajobs' },
    US: { flag: '🇺🇸', name: 'the US', tag: 'usjobs' },
    CA: { flag: '🇨🇦', name: 'Canada', tag: 'canadajobs' },
};

// The values most jobs share, most common first; ties go by `rank` (lower first), then by name.
function mostCommon(values, n, rank = () => 0) {
    const counts = new Map();
    for (const v of values.filter(Boolean)) counts.set(v, (counts.get(v) ?? 0) + 1);
    return [...counts].sort((a, b) => b[1] - a[1] || rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0])).slice(0, n).map(([v]) => v);
}

const MAX_TAG = 20;   // longer ones are nobody's hashtag (#greaterkolkataareajobs)
const COMPANY_TAGS = 3;

/**
 * A post's hashtags, at most `max`, each picked on purpose: the configured core ones (#hiring, #layoffs...),
 * #freshers when a job is entry level (else #opentowork), three brand tags (companies.hashtag, job.company_tag): the
 * post's most listed companies first, then the board's top ones (`boardTags`), the most common role, the two most
 * common cities (or #remotejobs) and the main country. A slot with nothing to say is left out, never padded.
 */
export function postHashtags(jobs, core = [], max = 12, boardTags = []) {
    const entry = jobs.some((j) => /^(entry level|internship)$/i.test(j.seniority ?? ''));
    const cities = jobs.filter((j) => j.workplace !== 'remote').map((j) => where(j));
    const remote = jobs.filter((j) => j.workplace === 'remote').length > cities.length;
    // Equally listed companies: the ones with the most jobs on the board first.
    const own = mostCommon(jobs.map((j) => j.company_tag), COMPANY_TAGS, (t) => (boardTags.includes(t) ? boardTags.indexOf(t) : boardTags.length));
    return [...new Set([
        ...core,
        entry ? 'freshers' : 'opentowork',
        ...[...own, ...boardTags.filter((t) => !own.includes(t))].slice(0, COMPANY_TAGS),
        ...mostCommon(jobs.map((j) => ROLE_TAGS[j.role]), 1),
        ...(remote ? ['remotejobs'] : []),
        ...mostCommon(cities.map(cityTag), 2),
        ...mostCommon(jobs.map((j) => COUNTRIES[j.country]?.tag), 1),
    ].map((t) => hashtag(t ?? '')).filter((t) => t && t.length <= MAX_TAG))].slice(0, max);
}

/** A job title cut at a word to at most `max` characters: "Software Development Engineer II, Amazon FinAuto…". */
export function shortTitle(title, max = 70) {
    const t = String(title ?? '').trim().replace(/\s+/g, ' ');
    return t.length <= max ? t : `${t.slice(0, max).replace(/[\s,;:(–|/-]+[^\s,;:(–|/-]*$/, '')}…`;
}

/** Jobs on the board as the post says them: 21609 → "21,600+", 850 → "850". */
export function liveCount(n) {
    return n >= 1000 ? `${(Math.floor(n / 100) * 100).toLocaleString('en-US')}+` : String(n);
}

/** ['IN', 'US', 'CA'] → "India, the US and Canada" (countries the post has no name for are left out). */
export function countryList(codes) {
    const names = codes.map((c) => COUNTRIES[c]?.name).filter(Boolean);
    return names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names.at(-1)}` : names[0] ?? 'several countries';
}

// 2450000 INR → "₹24L", 850000 → "₹8.5L"; 132000 USD → "$132k".
function money(amount, currency) {
    if (currency === 'INR') return `₹${amount >= 1e6 ? Math.round(amount / 1e5) : Math.round(amount / 1e4) / 10}L`;
    const k = `${Math.round(amount / 1000)}k`;
    return { USD: `$${k}`, EUR: `€${k}`, GBP: `£${k}` }[currency] ?? `${currency} ${k}`;
}

/**
 * The pay line for a job: the salary its listing shows, else the Glassdoor median for its role at its company
 * (rows from company_salaries: { company_slug, query, currency, median }), else null.
 */
export function payFor(job, salaries = []) {
    // A plain string, or a list stored as text ('["₹20L","₹30L"]' or '{₹20L,₹30L}') shown as a range.
    const raw = String(job.salary ?? '').trim();
    const listed = /^[[{]/.test(raw) ? raw.replace(/[[\]{}"]/g, '').split(/\s*,\s*/).filter(Boolean).join(' – ') : raw;
    if (listed && listed.length <= 40) return listed;
    const mine = salaries.filter((s) => s.company_slug === job.company_slug && s.median > 0);
    for (const role of payRoles(job)) {
        const row = mine.find((s) => s.query === role);
        if (row) return `~${money(Number(row.median), row.currency)}/yr on Glassdoor`;
    }
    return null;
}

/**
 * The link for a job in a post: its page on our site (/job.html?id=<job id>, with an "Apply on LinkedIn" button)
 * when siteUrl is given, else a short linkedin.com/jobs/view/<id> link when it is a LinkedIn job, else its apply link.
 */
export function jobLink({ job_id: jobId, url, apply_url: applyUrl }, siteUrl) {
    if (siteUrl && jobId) return `${siteUrl.replace(/\/$/, '')}/job.html?id=${encodeURIComponent(jobId)}`;
    const id = String(url ?? '').match(/linkedin\.com\/jobs\/view\/(?:[^/?#]*-)?(\d{6,})/)?.[1];
    if (id) return `https://www.linkedin.com/jobs/view/${id}`;
    return /^https?:\/\//.test(url ?? '') ? url : /^https?:\/\//.test(applyUrl ?? '') ? applyUrl : null;
}

/** One role of the post: "🇮🇳 𝗚𝗼𝗼𝗴𝗹𝗲 — Software Engineer II, Bengaluru" (company in bold, a 🌍 when the country is unknown). */
function roleLine(job) {
    return `${COUNTRIES[job.country]?.flag ?? '🌍'} ${bold(job.company)} — ${shortTitle(job.title)}${where(job) ? `, ${where(job)}` : ''}`;
}

/**
 * How long ago the oldest job in the post was first seen, rounded up and said short: "40 minutes", "9 hours",
 * "2 days". Jobs without first_seen_at count as just seen.
 */
export function freshness(jobs, now = new Date()) {
    const oldest = Math.min(...jobs.map((j) => (j.first_seen_at ? new Date(j.first_seen_at) : now).getTime()));
    const minutes = Math.max(1, Math.ceil((now.getTime() - oldest) / 60000));
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'}`;
    const hours = Math.ceil(minutes / 60);
    if (hours < 48) return `${hours} hour${hours === 1 ? '' : 's'}`;
    return `${Math.ceil(hours / 24)} days`;
}

/**
 * The post text: a bold hook against dead listings; a line per job (flag, company in bold, role, city) with its page
 * on the site under it (see jobLink); why fresh matters, what the scraper does and how many jobs `board` ({ live,
 * countries }) has; the follow ask; the board's link (free, said plainly so it doesn't read as a sales pitch); hashtags. Job titles and hashtags stay plain so LinkedIn search finds them.
 */
export function buildPost({ jobs, board, siteUrl, hashtags = [], maxHashtags = 12, boardTags = [], scrapeEvery, followAsk, now = new Date() }) {
    const count = liveCount(board.live);
    const tags = postHashtags(jobs, hashtags, maxHashtags, boardTags);
    // Over LinkedIn's limit: drop hashtags from the end first (down to 3), before any job has to go.
    let body = text(tags);
    while (body.length > MAX_CHARS && tags.length > 3) {
        tags.pop();
        body = text(tags);
    }
    return body;

    function text(tags) {
        return [
            `🚨 ${bold('STOP APPLYING TO DEAD JOB LISTINGS.')}`,
            '',
            `⏱️ Just found at top companies, all in the last ${freshness(jobs, now)}:`,
            ...jobs.flatMap((j) => [roleLine(j), `👉 ${jobLink(j, siteUrl)}`]),
            '',
            // Curly quotes: n8n's LinkedIn node sends straight ones as &#34;.
            'A posting with “500+ applicants” from 2 weeks ago sends your resume into an unread ATS void. The fix: apply early, while the job is fresh.',
            '',
            `I built a free scraper that indexes fresh tech roles ${scrapeEvery}. Right now ${count} are live across ${countryList(board.countries)}.`,
            '',
            `⚡ ${bold(followAsk)}`,
            `🔎 Free, no sign-up: all ${count} live jobs with direct apply links at ${siteUrl.replace(/\/$/, '')}/`,
            '',
            tags.map((t) => `#${t}`).join(' '),
        ].join('\n');
    }
}

/**
 * Problems with a post compared with the template, empty when it's fine. n8n runs the same checks before it
 * asks for approval (posts/n8n/hiring-alert-workflow.json, node "Check against template"); keep the two in step.
 * Bold and italic letters count as plain ones.
 */
export function checkPost(body) {
    const problems = [];
    const text = plain(body);
    const lines = text.split('\n');
    if (!/^🚨 STOP APPLYING TO DEAD JOB LISTINGS\./.test(lines[0] ?? '')) problems.push('first line is not "🚨 STOP APPLYING TO DEAD JOB LISTINGS."');
    // Counted on the raw text, where a bold letter takes two units, to stay safely under LinkedIn's limit.
    const length = String(body ?? '').length;
    if (length > MAX_CHARS) problems.push(`${length} characters, LinkedIn allows ${MAX_CHARS}`);
    const jobs = lines.filter((l) => /^(\p{Regional_Indicator}{2}|🌍) .+ — .+/u.test(l)).length;
    if (jobs < 1 || jobs > 8) problems.push(`${jobs} role lines ("🇮🇳 Company — Role, City"), expected 1 to 8`);
    const links = lines.filter((l) => /^👉 https?:\/\/\S+$/.test(l)).length;
    if (links !== jobs) problems.push(`${links} job links ("👉 https://…") for ${jobs} roles, expected one under each role`);
    if (!/^🔎 .+ https?:\/\/\S+$/.test(lines.at(-3) ?? '')) problems.push('the line before the hashtags should link to the board ("🔎 … https://…")');
    const tags = (lines.at(-1) ?? '').match(/#\w+/g) ?? [];
    if (tags.length < 1 || tags.length > 30) problems.push(`${tags.length} hashtags on the last line, expected 1 to 30`);
    if (/\b(undefined|null|NaN)\b/.test(text)) problems.push('contains "undefined", "null" or "NaN"');
    return problems;
}

async function ping(url, token, payload) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token && { Authorization: `Bearer ${token}` }) },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(20000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

// Draws the picture of every draft post that has none yet (new posts, and drafts from before pictures): a card
// for each of its companies with their roles, and a strip of the board's top premium companies.
async function drawImages(store, siteUrl) {
    const { renderPostImage, loadLogos, pickCompanies, MAX_FEATURED, MAX_STRIP } = await import('./post-image.js');
    const { rows } = await store.query(
        `SELECT p.id, p.created_at, COALESCE(c.slug, j.company) AS slug, COALESCE(c.name, j.company) AS name,
                c.logo_url, c.domain, COALESCE(c.is_premium, false) AS is_premium, j.title, j.location, j.workplace
         FROM posts p
         JOIN post_jobs pj ON pj.post_id = p.id
         JOIN jobs j ON j.source = pj.source AND j.job_id = pj.job_id
         LEFT JOIN companies c ON c.slug = j.company_slug
         WHERE p.status = 'draft' AND p.image IS NULL
         ORDER BY p.id, pj.position`);
    if (!rows.length) return;
    const { rows: premium } = await store.query(
        `SELECT c.slug, c.name, c.logo_url, c.domain, true AS is_premium
         FROM companies c LEFT JOIN company_index ci ON ci.slug = c.slug
         WHERE c.is_premium ORDER BY ci.score DESC NULLS LAST, c.sort_order, c.name LIMIT $1`, [MAX_STRIP + MAX_FEATURED]);
    const logos = new Map();   // slug → company with its logo, fetched once for all posts
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
        await store.query('UPDATE posts SET image = $2 WHERE id = $1', [id, png]);
        console.log(`  🖼 post ${id}: picture with ${featured.length} cards and ${strip.length} more logos`);
    }
}

async function main() {
    const { linkedin: cfg } = await import('./config.js');
    const { default: pg } = await import('pg');
    if (!cfg.people.length || !cfg.siteUrl) {
        console.log('No linkedin.people or linkedin.siteUrl in config, skipping LinkedIn posts');
        return;
    }
    if (!process.env.DATABASE_URL) throw new Error('Missing env var DATABASE_URL (a GitHub Actions secret)');
    // Supabase requires TLS; set DATABASE_SSL=false for a local Postgres without it.
    const store = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 2,
        ssl: process.env.DATABASE_SSL === 'false' ? false : { rejectUnauthorized: false } });
    const made = [];
    try {
        // scrap-py sets jobs.category rather than role; it gives the role hashtags (ai → #machinelearning).
        // post_candidates holds only entry and mid level software jobs at tier 1 premium companies (base.sql). Jobs
        // found in the last 6 hours come first (the heading says how fresh they are), then the first job of each
        // company, so a post names as many different companies as it can.
        const { rows: candidates } = await store.query(
            `SELECT pc.*, COALESCE(pc.role, CASE j.category WHEN 'ai' THEN 'ml' ELSE j.category END) AS role,
                    j.seniority, c.hashtag AS company_tag,
                    (SELECT l.country FROM job_search_hits h JOIN locations l ON l.id = h.location_id
                     WHERE h.source = j.source AND h.job_id = j.job_id ORDER BY l.priority, l.id LIMIT 1) AS country
             FROM post_candidates pc JOIN jobs j USING (source, job_id) LEFT JOIN companies c ON c.slug = pc.company_slug
             ORDER BY pc.first_seen_at > now() - interval '6 hours' DESC, pc.nth_at_company, pc.score DESC, pc.first_seen_at DESC LIMIT $1`, [cfg.jobsPerPost * cfg.people.length]);
        const { rows: salaries } = await store.query(
            `SELECT company_slug, query, currency, median_annual::float AS median FROM company_salaries
             WHERE company_slug = ANY($1) ORDER BY median_annual DESC`, [[...new Set(candidates.map((j) => j.company_slug))]]);
        for (const job of candidates) job.pay = payFor(job, salaries);
        // What the post says about the board: its live jobs and the countries they are in, and the brand tags of the
        // top premium companies (most jobs this week) to fill the hashtags.
        const { rows: [{ live }] } = await store.query(
            `SELECT COUNT(*)::int AS live FROM jobs WHERE status <> 'skipped' AND closed_at IS NULL`);
        const { rows: countries } = await store.query(
            `SELECT l.country FROM job_search_hits h JOIN locations l ON l.id = h.location_id
             JOIN jobs j ON j.source = h.source AND j.job_id = h.job_id
             WHERE j.status <> 'skipped' AND j.closed_at IS NULL AND l.country IS NOT NULL
             GROUP BY l.country HAVING COUNT(DISTINCT (h.source, h.job_id)) >= 50
             ORDER BY COUNT(DISTINCT (h.source, h.job_id)) DESC`);
        const { rows: top } = await store.query(
            `SELECT c.hashtag FROM companies c JOIN company_index ci ON ci.slug = c.slug
             WHERE c.premium_tier = 1 AND c.hashtag IS NOT NULL ORDER BY ci.jobs_week DESC, c.name LIMIT 8`);
        const board = { live, countries: countries.map((r) => r.country) };
        const options = { board, siteUrl: cfg.siteUrl, hashtags: cfg.hashtags, maxHashtags: cfg.maxHashtags, boardTags: top.map((r) => r.hashtag),
            scrapeEvery: cfg.scrapeEvery, followAsk: cfg.followAsk };
        const split = splitJobs(candidates, cfg.people, cfg.jobsPerPost);
        for (const person of cfg.people) {
            let jobs = split.get(person.id);
            const { rows: [last] } = await store.query(
                `SELECT EXTRACT(EPOCH FROM now() - max(created_at)) / 3600 AS hours FROM posts WHERE person = $1`, [person.id]);
            if (process.env.FORCE_POSTS !== 'yes' && last?.hours != null && last.hours < cfg.everyHours - 0.5) {   // half an hour of slack: scrape start times drift
                console.log(`  ${person.id}: last post ${Number(last.hours).toFixed(1)}h ago, next after ${cfg.everyHours}h`);
                continue;
            }
            if (jobs.length < cfg.minJobs) {
                console.log(`  ${person.id}: ${jobs.length} new job(s), fewer than ${cfg.minJobs}, no post`);
                continue;
            }
            let body = buildPost({ jobs, ...options });
            // Over LinkedIn's limit (very long titles): leave the last jobs for the next post, down to minJobs.
            while (body.length > MAX_CHARS && jobs.length > cfg.minJobs) {
                jobs = jobs.slice(0, -1);
                body = buildPost({ jobs, ...options });
            }
            // One transaction, so a failure (such as a schema the scrape hasn't applied yet) leaves no empty draft behind.
            const tx = await store.connect();
            let id;
            try {
                await tx.query('BEGIN');
                ({ rows: [{ id }] } = await tx.query('INSERT INTO posts (person, body) VALUES ($1, $2) RETURNING id',
                    [person.id, body]));
                for (const [i, j] of jobs.entries()) {
                    await tx.query('INSERT INTO post_jobs (post_id, source, job_id, position) VALUES ($1, $2, $3, $4)',
                        [id, j.source, j.job_id, i + 1]);
                }
                await tx.query('COMMIT');
            } catch (err) {
                await tx.query('ROLLBACK').catch(() => {});
                if (err.code === '42703') err.message += ' (the database schema is older than this code: run a scrape first, it applies the schema)';
                throw err;
            } finally {
                tx.release();
            }
            const problems = checkPost(body);
            if (problems.length) console.warn(`  ! post ${id} doesn't match the template: ${problems.join('; ')}`);
            made.push({ person, id, jobs: jobs.length });
            console.log(`  ✔ ${person.id}: post ${id} with ${jobs.length} job(s)`);
        }
        await drawImages(store, cfg.siteUrl);
    } finally {
        await store.end();
    }

    for (const { person, id, jobs } of made) {
        const key = person.id.toUpperCase().replace(/[^A-Z0-9]/g, '_');
        const url = process.env[`N8N_WEBHOOK_${key}`];
        if (!url) {
            console.log(`  ${person.id}: N8N_WEBHOOK_${key} not set, post ${id} is only on the posts page`);
            continue;
        }
        try {
            await ping(url, process.env[`POST_TOKEN_${key}`], {
                postId: Number(id), person: person.id, jobs, alertUrl: `${cfg.siteUrl.replace(/\/$/, '')}/alert.html?id=${id}`,
            });
            console.log(`  ↪ ${person.id}: n8n pinged for post ${id}`);
        } catch (err) {
            // The post stays a draft on the posts page; only the status code is logged, since logs are public.
            console.warn(`  ! ${person.id}: couldn't ping n8n (${err.message})`);
        }
    }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
    await main();
}

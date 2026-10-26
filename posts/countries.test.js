import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildPost, checkPost, plain } from './linkedin-posts.js';
import { boardUrl, localize } from './countries.js';
import { countryPosts } from './countries-config.js';

const site = 'https://krawlr.xyz';
const job = (n, extra = {}) => ({ job_id: `j${n}`, title: `Software Engineer ${n}`, company: `Co ${n}`, company_tag: `co${n}`,
    location: 'Seattle, Washington, United States', country: 'US', tags: [], ...extra });
const options = (country, extra = {}) => ({ board: { live: 2962, countries: [country] }, siteUrl: site, hashtags: ['hiring'],
    scrapeEvery: 'every 2 minutes', followAsk: 'Follow me.', ...extra });

test('board link filters to the country', () => {
    assert.equal(boardUrl(site, ['United States', 'San Francisco Bay Area']), 'https://krawlr.xyz/?places=United%20States,San%20Francisco%20Bay%20Area');
    assert.equal(boardUrl(`${site}/`, []), 'https://krawlr.xyz/');
});

test('a US post says the US, links to its jobs and still passes the template check', () => {
    const jobs = [job(1, { seniority: 'Entry level' }), job(2), job(3, { location: 'New York, New York, United States' })];
    const body = localize(buildPost({ jobs, ...options('US') }), { siteUrl: site, flag: '🇺🇸', entryTag: 'newgrad', places: ['United States', 'Seattle'] });
    const lines = plain(body).split('\n');
    assert.deepEqual(checkPost(body), []);
    assert.ok(lines.includes('🇺🇸 Co 1 — Software Engineer 1, Seattle'));
    assert.match(body, /Right now 2,900\+ are live across the US\./);
    assert.equal(lines.at(-3), '🔎 Free, no sign-up: all 2,900+ live jobs with direct apply links at https://krawlr.xyz/?places=United%20States,Seattle');
    assert.match(lines.at(-1), /#newgrad\b/);
    assert.doesNotMatch(lines.at(-1), /#freshers\b/);
    assert.match(lines.at(-1), /#usjobs\b/);
});

test('a country linkedin-posts.js has no flag for gets its own', () => {
    const jobs = [1, 2, 3].map((n) => job(n, { country: 'GB', location: 'London, England, United Kingdom' }));
    const body = localize(buildPost({ jobs, ...options('GB') }), { siteUrl: site, flag: '🇬🇧' });
    assert.ok(plain(body).split('\n').includes('🇬🇧 Co 1 — Software Engineer 1, London'));
    assert.deepEqual(checkPost(body), []);
});

test('every account id is its own, and none is an India person', async () => {
    const { linkedin } = await import('./config.js');
    const ids = countryPosts.countries.flatMap((c) => c.accounts.map((a) => a.id));
    assert.equal(new Set(ids).size, ids.length);
    for (const p of linkedin.people) assert.ok(!ids.includes(p.id), `${p.id} posts for India already`);
    for (const id of ids) assert.match(id, /^[a-z0-9_]+$/);   // POST_TOKEN_<ID> and the n8n Settings use it as is
});

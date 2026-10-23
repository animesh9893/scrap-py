import { test } from 'node:test';
import assert from 'node:assert/strict';
import { splitJobs, buildPost, checkPost, freshness, plain, bold, italic, payFor, payRoles, jobLink, postHashtags, shortTitle, liveCount, countryList } from './linkedin-posts.js';
import { postImageSvg, renderPostImage, pickCompanies, loadLogos, MAX_FEATURED } from './post-image.js';

const people = [{ id: 'animesh' }, { id: 'sachin' }];
const job = (n, extra = {}) => ({ job_id: `j${n}`, title: `Engineer ${n}`, company: `Co ${n % 3}`, location: 'Bengaluru, Karnataka, India', country: 'IN', tags: [], ...extra });
const board = { live: 21609, countries: ['IN', 'US', 'CA'] };
const options = { board, siteUrl: 'https://jobs.example.com/', hashtags: ['hiring', 'techjobs'], scrapeEvery: 'every 2 minutes', followAsk: 'Follow me for 4 fresh drops a day.' };

test('jobs are dealt out in turn, different for each person', () => {
    const split = splitJobs([1, 2, 3, 4, 5].map((n) => job(n)), people, 2);
    assert.deepEqual(split.get('animesh').map((j) => j.job_id), ['j1', 'j3']);
    assert.deepEqual(split.get('sachin').map((j) => j.job_id), ['j2', 'j4']);
});

test('a short list goes to whoever still has room', () => {
    const split = splitJobs([1, 2, 3].map((n) => job(n)), people, 5);
    assert.equal(split.get('animesh').length + split.get('sachin').length, 3);
});

test('post follows the template and passes the check', () => {
    const jobs = [
        job(1, { company: 'Google', title: 'Software Engineer II', company_tag: 'google', role: 'backend', seniority: 'Entry level' }),
        job(2, { company: 'Amazon', title: 'SDE I', company_tag: 'amazon', country: 'US', location: 'Seattle, Washington, United States' }),
        job(3, { company: 'Cisco', title: 'Software Engineer', workplace: 'remote', location: 'Remote', country: 'CA' }),
    ];
    const body = buildPost({ jobs, ...options, boardTags: ['microsoft', 'amazon'] });
    const lines = body.split('\n');
    assert.equal(lines[0], '🚨 𝗦𝗧𝗢𝗣 𝗔𝗣𝗣𝗟𝗬𝗜𝗡𝗚 𝗧𝗢 𝗗𝗘𝗔𝗗 𝗝𝗢𝗕 𝗟𝗜𝗦𝗧𝗜𝗡𝗚𝗦.');
    assert.match(plain(body), /I built a free scraper that indexes fresh tech roles every 2 minutes\. Right now 21,600\+ are live across India, the US and Canada\./);
    assert.match(body, /“500\+ applicants”/);
    assert.deepEqual(lines.slice(2, 9).map(plain), [
        '⏱️ Just found at top companies, all in the last 1 minute:',
        '🇮🇳 Google — Software Engineer II, Bengaluru',
        '👉 https://jobs.example.com/job.html?id=j1',
        '🇺🇸 Amazon — SDE I, Seattle',
        '👉 https://jobs.example.com/job.html?id=j2',
        '🇨🇦 Cisco — Software Engineer, Remote',
        '👉 https://jobs.example.com/job.html?id=j3',
    ]);
    assert.equal(plain(lines.at(-3)), '🔎 Free, no sign-up: all 21,600+ live jobs with direct apply links at https://jobs.example.com/');
    assert.equal(plain(lines.at(-4)), '⚡ Follow me for 4 fresh drops a day.');
    assert.equal(lines.at(-1), '#hiring #techjobs #freshers #amazon #google #microsoft #backenddeveloper #bangalorejobs #seattlejobs #canadajobs');
    assert.deepEqual(checkPost(body), []);
});

test('freshness: the oldest job in the post, rounded up', () => {
    const now = new Date('2026-10-23T22:00:00Z');
    const at = (iso) => ({ first_seen_at: iso });
    assert.equal(freshness([at('2026-10-23T21:50:30Z'), at('2026-10-23T21:59:00Z')], now), '10 minutes');
    assert.equal(freshness([at('2026-10-23T13:30:00Z')], now), '9 hours');
    assert.equal(freshness([at('2026-10-21T10:00:00Z')], now), '3 days');
    assert.equal(freshness([{}], now), '1 minute');
});

test('counts, countries and long titles are said short', () => {
    assert.deepEqual([21609, 21600, 1000, 999, 5].map(liveCount), ['21,600+', '21,600+', '1,000+', '999', '5']);
    assert.equal(countryList(['IN', 'US', 'CA']), 'India, the US and Canada');
    assert.equal(countryList(['IN', 'XX']), 'India');
    assert.equal(countryList([]), 'several countries');
    assert.equal(shortTitle('Software Engineer II'), 'Software Engineer II');
    assert.equal(shortTitle('Software Development Engineer II, Amazon FinAuto - Travel & Events Platform Team (Payments)'),
        'Software Development Engineer II, Amazon FinAuto - Travel & Events…');
});

test('pay: the listing\'s own salary, else the Glassdoor median for the closest role', () => {
    const salaries = [
        { company_slug: 'acme', query: 'Software Engineer', currency: 'INR', median: 1850000 },
        { company_slug: 'acme', query: 'Senior Software Engineer', currency: 'INR', median: 3420000 },
        { company_slug: 'acme', query: 'Data Analyst', currency: 'INR', median: 850000 },
        { company_slug: 'globex', query: 'Backend Developer', currency: 'USD', median: 132400 },
    ];
    const pay = (extra) => payFor({ company_slug: 'acme', title: 'Engineer', role: 'backend', level: 'mid', ...extra }, salaries);
    assert.equal(pay({ salary: '₹20,00,000/yr - ₹30,00,000/yr' }), '₹20,00,000/yr - ₹30,00,000/yr');
    assert.equal(pay({ salary: '["₹20L","₹30L"]' }), '₹20L – ₹30L');
    assert.equal(pay({}), '~₹19L/yr on Glassdoor');
    assert.equal(pay({ level: 'senior' }), '~₹34L/yr on Glassdoor');
    assert.equal(pay({ role: 'data', title: 'Data Analyst' }), '~₹8.5L/yr on Glassdoor');
    assert.equal(pay({ role: 'data', title: 'Data Engineer' }), null);
    assert.equal(pay({ level: 'intern' }), null);
    assert.equal(pay({ company_slug: 'globex' }), '~$132k/yr on Glassdoor');
    assert.equal(pay({ company_slug: 'nobody' }), null);
    assert.deepEqual(payRoles({ title: 'Senior Android Engineer', role: 'mobile', level: 'senior' }),
        ['Senior Software Engineer', 'Android Developer', 'Mobile Developer', 'Software Engineer']);
});

test('job links: the job page on the site, else short LinkedIn job pages, else the apply link', () => {
    assert.equal(jobLink({ job_id: '4123456789', url: 'https://in.linkedin.com/jobs/view/x-4123456789' }, 'https://jobs.example.com/'),
        'https://jobs.example.com/job.html?id=4123456789');
    assert.equal(jobLink({ url: 'https://in.linkedin.com/jobs/view/sde-ii-at-acme-4123456789' }), 'https://www.linkedin.com/jobs/view/4123456789');
    assert.equal(jobLink({ url: 'https://boards.greenhouse.io/acme/jobs/1' }), 'https://boards.greenhouse.io/acme/jobs/1');
    assert.equal(jobLink({ url: null, apply_url: 'https://acme.com/careers/1' }), 'https://acme.com/careers/1');
    assert.equal(jobLink({ url: 'j1' }), null);
});

test('bold and italic letters turn back into plain ones', () => {
    const text = 'Hiring 2026: C++ & Go (Remote)';
    assert.equal(plain(bold(text)), text);
    assert.equal(plain(italic(text)), text);
    assert.equal(bold('Ab9'), '𝗔𝗯𝟵');
    assert.equal(italic('Ab9'), '𝘈𝘣9');
});

test('check catches posts that drift from the template', () => {
    assert.ok(checkPost('Hello world').length >= 3);
    const ok = buildPost({ jobs: [job(1)], ...options });
    assert.deepEqual(checkPost(ok), []);
    assert.match(checkPost(ok.replace('Engineer 1', 'undefined')).join(), /undefined/);
    assert.match(checkPost(ok.replace(/\n👉 .*/, '')).join(), /0 job links .* for 1 roles/);
    assert.match(checkPost(ok.replace('🇮🇳 ', '')).join(), /0 role lines/);
    assert.match(checkPost(ok.replace(/(🔎 .*) https:\S+/, '$1 link in bio')).join(), /link to the board/);
    assert.match(checkPost(ok + 'x'.repeat(3000)).join(), /characters/);
});

test('the n8n workflow runs the same template check', async () => {
    const { readFileSync } = await import('node:fs');
    const workflow = JSON.parse(readFileSync(new URL('./n8n/hiring-alert-workflow.json', import.meta.url)));
    const code = workflow.nodes.find((n) => n.name === 'Check against template').parameters.jsCode;
    assert.ok(code.includes(checkPost.toString()), 'copy checkPost() from posts/linkedin-posts.js into the workflow');
    assert.ok(code.includes(plain.toString()), 'copy plain() from posts/linkedin-posts.js into the workflow');
    assert.equal(code.split('function plain(').length, 2, 'plain() is in the workflow once');
});

test('hashtags: core, audience, three companies (the post\'s first, then the board\'s), role, cities, country', () => {
    const jobs = [
        job(1, { role: 'backend', company_tag: 'amazon', seniority: 'Mid-Senior level' }),
        job(2, { role: 'backend', company_tag: 'amazon', location: 'Greater Kolkata Area' }),
        job(3, { role: 'ml', company_tag: 'google', seniority: 'Entry level', location: 'Greater Kolkata Area' }),
        job(4, { role: 'backend', company_tag: null, location: 'Pune, Maharashtra, India' }),
    ];
    const core = ['hiring', 'TechJobs', 'layoffs'];
    assert.deepEqual(postHashtags(jobs, core, 12, ['microsoft', 'amazon', 'meta']),
        ['hiring', 'techjobs', 'layoffs', 'freshers', 'amazon', 'google', 'microsoft', 'backenddeveloper', 'kolkatajobs', 'bangalorejobs', 'indiajobs']);
    assert.equal(postHashtags(jobs, core, 4).length, 4);
    // No entry level job: #opentowork. Mostly remote: #remotejobs. A long or missing brand tag is left out, not padded.
    const remote = [job(1, { workplace: 'remote', company_tag: 'averyveryverylongcompanytag', country: 'US' }), job(2, { workplace: 'remote' })];
    assert.deepEqual(postHashtags(remote, ['hiring'], 12), ['hiring', 'opentowork', 'remotejobs', 'indiajobs']);
});

test('picture: the post\'s companies get cards, premium then most jobs first; the strip skips them', () => {
    const premium = Array.from({ length: 25 }, (_, i) => ({ slug: `p${i}`, name: `Premium ${i}`, is_premium: true }));
    const jobs = (n) => Array.from({ length: n }, (_, i) => ({ title: `Role ${i}`, place: 'Pune' }));
    const post = [
        { slug: 'small', name: 'Small Co', is_premium: false, jobs: jobs(1) },
        { slug: 'busy', name: 'Busy Co', is_premium: false, jobs: jobs(3) },
        { slug: 'p3', name: 'Premium 3', is_premium: true, jobs: jobs(1) },
    ];
    const { featured, more, strip } = pickCompanies(post, premium);
    assert.deepEqual(featured.map((c) => c.slug), ['p3', 'busy', 'small']);
    assert.equal(more, 0);
    assert.equal(strip.length, 8);
    assert.ok(!strip.some((c) => c.slug === 'p3'));
    assert.equal(pickCompanies(post.slice(0, 1), premium).strip.length, 16);   // few cards: two rows of logos
    const many = Array.from({ length: 9 }, (_, i) => ({ slug: `c${i}`, name: `Co ${i}`, jobs: jobs(1) }));
    assert.deepEqual([pickCompanies(many, premium).featured.length, pickCompanies(many, premium).more], [MAX_FEATURED, 3]);
});

test('picture: logos fall back to the site icon, then to initials, and text is escaped', async () => {
    const asked = [];
    const fetchLogo = async (url) => (asked.push(url), url.includes('good') ? 'data:image/png;base64,AAAA' : null);
    const [a, b, c] = await loadLogos([
        { name: 'A', logo_url: 'https://good/a' }, { name: 'B', logo_url: 'https://bad/b', domain: 'good.com' }, { name: 'AT&T <Labs>' },
    ], fetchLogo);
    assert.equal(a.logo, 'data:image/png;base64,AAAA');
    assert.equal(b.logo, 'data:image/png;base64,AAAA');
    assert.equal(c.logo, null);
    assert.match(asked[2], /s2\/favicons\?domain=good\.com/);
    const svg = postImageSvg({ featured: [{ ...a, jobs: [{ title: 'Dev <1>', place: 'Pune' }] }, { ...c, jobs: [{ title: 'QA' }, { title: 'SRE' }] }],
        more: 2, strip: [b], jobs: 3, companiesInPost: 4, siteUrl: 'https://jobs.example.com/', date: new Date('2026-10-20T10:00:00Z') });
    assert.match(svg, /AT&amp;T &lt;Labs&gt;/);
    assert.match(svg, />A&lt;</);   // initials when there's no logo
    assert.match(svg, />Dev &lt;1&gt;</);
    assert.match(svg, />2 roles</);
    assert.match(svg, />3 new openings</);
    assert.match(svg, /\+ 2 more companies in the post/);
    assert.match(svg, /ALSO HIRING/);
    assert.match(svg, /20 Oct 2026/);
    assert.match(svg, /jobs\.example\.com</);
    const png = renderPostImage({ featured: [{ ...c, jobs: [] }], jobs: 1, companiesInPost: 1, siteUrl: 'https://jobs.example.com' });
    assert.deepEqual([...png.subarray(1, 4)], [...Buffer.from('PNG')]);
});

test('a long post drops hashtags before going over LinkedIn\'s limit', () => {
    const jobs = Array.from({ length: 8 }, (_, i) => job(i, { title: 'Senior Software Engineer II, Backend Platform (Golang) '.repeat(3),
        company: `Cencora Global Capability Center ${i}`, tags: ['Go', 'Kubernetes'] }));
    const core = Array.from({ length: 30 }, (_, i) => `longhashtag${i}`);
    const body = buildPost({ jobs, ...options, hashtags: core, maxHashtags: 30 });
    assert.ok(body.length <= 3000, `${body.length} characters`);
    assert.deepEqual(checkPost(body), []);
});

// Netlify function: each person's n8n reports here what happened to their LinkedIn post, after the
// approval email. POST /api/post-status with header "Authorization: Bearer <token>" and JSON
// { "id": 12, "status": "posted" | "skipped", "linkedinUrl": "https://www.linkedin.com/feed/update/…" }.
// The token is the post's person's own (Netlify env POST_TOKEN_<ID>, e.g. POST_TOKEN_ANIMESH), so one person's
// n8n can't change the other's posts. Only drafts change. Also needs SUPABASE_URL and SUPABASE_SECRET_KEY.
import { timingSafeEqual } from 'node:crypto';

export const config = { path: '/api/post-status' };

const env = (name) => {
    const v = process.env[name];
    if (!v) throw new Error(`Missing env var ${name}`);
    return v;
};

const same = (a, b) => {
    const x = Buffer.from(String(a)), y = Buffer.from(String(b));
    return x.length === y.length && timingSafeEqual(x, y);
};

const reply = (body, status = 200) => Response.json(body, { status });

export default async function handler(req) {
    if (req.method !== 'POST') return reply({ ok: false }, 405);
    const { id, status, linkedinUrl = null } = await req.json().catch(() => ({}));
    if (!Number.isInteger(Number(id)) || !['posted', 'skipped'].includes(status)
        || (linkedinUrl !== null && !/^https:\/\/(www\.)?linkedin\.com\//.test(linkedinUrl))) {
        return reply({ ok: false, error: 'Send { id, status: posted | skipped, linkedinUrl? }' }, 400);
    }
    try {
        const base = `${env('SUPABASE_URL').replace(/\/$/, '')}/rest/v1/posts`;
        const key = env('SUPABASE_SECRET_KEY');
        // New sb_secret_ keys go only in apikey; a legacy service_role JWT also works as a bearer token.
        const headers = { apikey: key, 'Content-Type': 'application/json', ...(key.startsWith('eyJ') && { Authorization: `Bearer ${key}` }) };

        const found = await fetch(`${base}?id=eq.${Number(id)}&select=person,status`, { headers });
        if (!found.ok) throw new Error(`Supabase read ${found.status}`);
        const [post] = await found.json();
        const token = post && process.env[`POST_TOKEN_${post.person.toUpperCase().replace(/[^A-Z0-9]/g, '_')}`];
        const given = (req.headers.get('authorization') ?? '').replace(/^Bearer\s+/i, '');
        if (!token || !given || !same(given, token)) return reply({ ok: false }, 401);
        if (post.status !== 'draft') return reply({ ok: false, error: `Post is already ${post.status}` }, 409);

        const saved = await fetch(`${base}?id=eq.${Number(id)}&status=eq.draft`, {
            method: 'PATCH', headers: { ...headers, Prefer: 'return=minimal' },
            body: JSON.stringify({ status, linkedin_url: linkedinUrl, posted_at: status === 'posted' ? new Date().toISOString() : null }),
        });
        if (!saved.ok) throw new Error(`Supabase save ${saved.status}`);
        return reply({ ok: true });
    } catch (err) {
        console.error(err);
        return reply({ ok: false }, 500);
    }
}

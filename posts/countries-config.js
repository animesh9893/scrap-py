// LinkedIn posts for readers outside India (countries.js, .github/workflows/country-posts.yml): the same template as
// the India posts (config.js, linkedin-posts.js), but each post names only jobs in one country, says how many are
// live there and links to the board filtered to that country. Each account is its own LinkedIn profile with its own
// n8n workflow (n8n/country-alert-workflow.json) and token (Netlify env POST_TOKEN_<ID>). The India posts don't read
// this file.
export const countryPosts = {
    siteUrl: 'https://krawlr.xyz',
    everyHours: 4,      // at most one post per account this often
    jobsPerPost: 5,
    minJobs: 3,
    scrapeEvery: 'every 2 minutes',                             // keep it true
    followAsk: 'Follow me for multiple fresh drops a day.',
    maxHashtags: 12,
    countries: [
        {
            code: 'US',             // locations.country: a job is in the US when a search of a US location found it
            name: 'the US',         // "Right now 2,900+ are live across the US."
            flag: '🇺🇸',
            // These first, then the same picked tags as India's: brand tags, role, cities, #usjobs. #freshers is an
            // Indian word; here it becomes the tag below.
            hashtags: ['hiring', 'techjobs', 'softwareengineering', 'layoffs'],
            entryTag: 'newgrad',
            accounts: [{ id: 'usa', name: 'US account' }],
        },
        {
            code: 'CA',
            name: 'Canada',
            flag: '🇨🇦',
            hashtags: ['hiring', 'techjobs', 'softwareengineering', 'canadajobs'],
            entryTag: 'newgrad',
            accounts: [{ id: 'canada', name: 'Canada account' }],
        },
        // Another country: seed its locations (seed.sql) so the scraper searches it, then add it here with its own
        // flag and country hashtag (e.g. 'ukjobs') in `hashtags`, and an account.
    ],
};

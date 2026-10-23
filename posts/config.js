// LinkedIn posts (linkedin-posts.js): the hook against dead listings, the live job count, a few fresh roles at top
// companies and the follow ask. Every job links to its /job.html page on siteUrl, under its line in the post.
export const linkedin = {
    siteUrl: 'https://krawlr.xyz',
    everyHours: 4,      // at most one post per person this often; runs in between keep their jobs for the next post
    jobsPerPost: 5,     // roles named in a post
    minJobs: 3,         // no post for a person who would get fewer new jobs than this; they wait for the next run
    scrapeEvery: 'every 2 minutes',                      // "My scraper indexes fresh tech roles ..."; keep it true
    followAsk: 'Follow me for multiple fresh drops a day.',     // match how often posts really go out
    // Hashtags picked on purpose: these, then #freshers or #opentowork, three top companies' own tags
    // (companies.hashtag, premium.sql), the main role, the main cities and country (postHashtags in linkedin-posts.js).
    hashtags: ['hiring', 'techjobs', 'softwarejobs', 'layoffs'],
    maxHashtags: 12,
    people: [
        { id: 'animesh', name: 'Animesh' },
        { id: 'sachin', name: 'Sachin' },
    ],
};

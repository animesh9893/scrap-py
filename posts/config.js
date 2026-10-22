// LinkedIn hiring-alert posts (linkedin-posts.js). siteUrl is the job board: each job links to its /job.html page
// and the post ends with the home page.
export const linkedin = {
    siteUrl: 'https://krawlr.xyz',
    everyHours: 4,      // at most one post per person this often; runs in between keep their jobs for the next post
    jobsPerPost: 10,
    minJobs: 10,        // no post for a person who would get fewer new jobs than this; they wait for the next run
    subtitle: '💼 Entry and mid level software roles at top companies',   // the line under the heading
    // A few hashtags picked on purpose: these, then #freshers or #opentowork, the two biggest companies' own tags
    // (companies.hashtag, premium.sql), the main role and the main city (postHashtags in linkedin-posts.js).
    hashtags: ['hiring', 'softwarejobs', 'techjobs'],
    maxHashtags: 8,
    people: [
        { id: 'animesh', name: 'Animesh' },
        { id: 'sachin', name: 'Sachin' },
    ],
};

// LinkedIn hiring-alert posts (linkedin-posts.js). siteUrl is the job board: each job links to its /job.html page
// and the post ends with the home page.
export const linkedin = {
    siteUrl: 'https://krawlr.xyz',
    everyHours: 4,      // at most one post per person this often; runs in between keep their jobs for the next post
    jobsPerPost: 10,
    minJobs: 10,        // no post for a person who would get fewer new jobs than this; they wait for the next run
    hashtags: ['hiring', 'softwareengineering', 'techjobs', 'jobsearch'],
    maxHashtags: 25,    // the hashtags above first, then the jobs' roles, tech, cities and companies, then filler ones
    fillerHashtags: ['jobs', 'nowhiring', 'jobopening', 'careers', 'jobseekers', 'opentowork', 'softwaredeveloper',
        'softwarejobs', 'itjobs', 'developers', 'programming', 'technology', 'engineering', 'freshers', 'career'],
    people: [
        { id: 'animesh', name: 'Animesh' },
        { id: 'sachin', name: 'Sachin' },
    ],
};

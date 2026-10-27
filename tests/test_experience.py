"""experience.py against how real job descriptions on LinkedIn state (or seem to state) years of experience."""

import pytest

import experience

# (one line of a description, the years it asks for as (min, max), or None when it isn't a requirement)
LINES = [
    ("isit https://www.solitontech.com/and read the Impact Report to get a glimpse of the first 25 years of our truly meaningful journey.", None),
    ("* 14+ years of overall experience with a minimum of 8 years on the Salesforce platform", (14,None)),
    ("**Experience: 8-15 Yrs**", (8,15)),
    ("* Bachelor's Degree and 8+ years in SRE, platform, or software engineering roles w", (8,None)),
    ("Experience: 3-5 Years   ", (3,5)),
    ("* 3+ years of non-internship professional software developme", (3,None)),
    ("Mid-level engineers will generally bring roughly 3–5 years of relevant engineering experience.", (3,5)),
    ("* 15 years of full-time education with minimum 50%", None),
    ("* Experience: 3–5+ years of relevant experience in product analysis", (3,5)),
    ("Experience:1–3 Years   ", (1,3)),
    ("* Data Engineering (4+ years)", (4,None)),
    ("**Experience Qualifications** Typically < 2 years in data analysis, reporting", (0,2)),
    ("* Minimum of 2 years in software QA with automation experience", (2,None)),
    ("Experience 10 Years", (10,None)),
    ("* Overall 15 years and typically 6-8 years in technology architecture", (15,None)),
    (".S. News & World Report as a top 10 best Children's Hospitals in the nation for more than 15 years", None),
    ("* 2-3 Years' Experience with knowledge in inventory Control s", (2,3)),
    ("ainable future for all of us. With more than 200 GW of wind power installed worldwide and 40+ years of experience in wind energy, we have", None),
    ("**Experience (in years):**  4 to 5 years of professional software", (4,5)),
    ("h university. An institution that has become a beacon of academic excellence in less than 10 years since its inception.", None),
    ("**Minimum 12 Year(s) Of Experience Is Required** **Educational Qualification :**  15 years full time education   ", (12,None)),
    ("* A 15 years full time education is required.", None),
    ("* **4-8 years**  of experience in AI Engineering", (4,8)),
    ("* Ten or more years of experience as a designer", (10,None)),
    ("nts. We are also proud to be recognized by Built In as a Best Place to Work in Technology 3 years in a row.", None),
    ("* 13+years of Proven Experience: Extensive background", (13,None)),
    ("Will have experience of working in security focussed roles. Likely will have greater than 5 years full time in security, in an overall career", (5,None)),
    ("* Work experience required- 3 years and above", (3,None)),
    ("* 4+ years as an engineer, with strong systems instincts", (4,None)),
    ("s ambition is why McDonald’s remains one of the world’s leading corporations after almost 70 years.", None),
    ("**Teamware Solutions**  (a division of Quantum Leap Consulting Pvt. Ltd.) is a 20+ year industry leader", None),
    ("ity and maintainability of our software stack which has to be maintained for a long time (10+ years).", None),
    ("**Minimum 7.5 Year(s) Of Experience Is Required**", (7,None)),
    ("* Minimum of eight (8) years of experience as a data engineer", (8,None)),
    ("* 1+ yr prac. exp. w/ AI-native coding tool", (1,None)),
    ("Exp- min 3 to 15 yrs,   ", (3,15)),
    ("**The opportunity** Guidewire Policy Centre Support with 4-7 Yrs   ", (4,7)),
    ("* Bachelor's Degree preferred with up to 3 years of relevant experience.", (0,3)),
    ("* Any Graduate/MBA with 6months to 1 years of relevant work experience", (0,1)),
    ("ing, Electrical Engineering, or related field of study and Five (5) years of experience as a Software Engineer", (5,None)),
    ("* 25+ Years of Proven Industry Experience in 4 major markets", None),
    ("* ClearlyRated's Best of Staffing® Client and Talent Award winner 12 years in a row.", None),
    ("* Several years of software engineering experience (3 years or more)", (3,None)),
    ("* 8+ yrs. of overall experience working in web analytics", (8,None)),
    ("3+ years Artifactory or a comparable artifact repository", (3,None)),
    ("* MS + ~3 years or BS + ~5 years of experience", (3,None)),
    ("• Post qualification 3-5 Years in Statutory Audit", (3,5)),
    ("We're innovating fearlessly for 40 years to create solutions", None),
    ("Freshers are welcome to apply", (0,0)),
    ("freshers please do not apply", None),
    ("You must be 18 years old", None),
    ("This is a 2 year contract role", None),
    ("Requires a 4-year degree", None),
    ("* Minimum 1 year of full-time professional experience", (1,None)),
    ("Experience: 0-2 years", (0,2)),
    ("Experience - 6 months+", (0,None)),
    ("Join a company with 30 years of history", None),
    ("We have been serving clients for over 15 years", None),
    ("Founded 12 years ago", None),
]


@pytest.mark.parametrize("line, want", LINES)
def test_lines(line, want):
    got = experience.years("", "", line)
    assert ((got["min"], got["max"]) if got else None) == want


def test_label_beats_the_first_mention():
    text = ("We have been a leader for 30 years.\n* 2+ years of Python\n**Experience:** 4-6 Years\n"
            "* 16+ years of overall professional experience & 10+ years of full-stack experience")
    assert experience.years("", "", text) == {"min": 4, "max": 6}


def test_title_then_requirements_then_description():
    assert experience.years("Python Developer 3-4 years Exp", "", "* 5+ years of Python") == {"min": 3, "max": 4}
    assert experience.years("Engineer", "* 6+ years of Go", "With 2+ years of experience in Go\n* 6+ years of Go") \
        == {"min": 6, "max": None}
    assert experience.years("Engineer", "", "With 2+ years of experience in Go") == {"min": 2, "max": None}


def test_open_range_takes_a_matching_closed_one():
    assert experience.years("Senior AI Engineer | 5+ Yrs", "", "• 5 to 8 years of total software experience") \
        == {"min": 5, "max": 8}


def test_requirements_matched_to_the_description_as_written():
    # split_sections() makes "**1 year of experience**" a heading line, losing the "up to" before it
    description = "* Recent graduate or up to  **1 year of experience**  ."
    assert experience.years("", "**1 year of experience**", description) == {"min": 0, "max": 1}


def test_months_need_experience():
    assert experience.years("", "", "* 84 months experience in capacity planning") == {"min": 7, "max": None}
    assert experience.years("", "", "* By 6 months you will understand the media stack and user experience") is None


def test_nothing_said():
    assert experience.years("Engineer", "", "Build things with us.") is None
    assert experience.years("Engineer", "", None) is None

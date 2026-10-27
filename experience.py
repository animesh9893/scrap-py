"""Years of experience a job asks for, read from its title and description (LinkedIn's "Seniority level" is too
broad: "Mid-senior level" covers most jobs).

    years(title, asks, description) -> {"min": 3, "max": 5}    "3-5 years of experience"
                                        {"min": 5, "max": None} "5+ years", "at least 5 years", "5 years and above"
                                        {"min": 0, "max": 2}    "up to 2 years", "< 2 years", "6 months to 2 years"
                                        {"min": 0, "max": 0}    "freshers welcome", with no years given
                                        None                    when it doesn't say

Every "N years" in the text is a candidate. A candidate counts only when the words around it say it is
experience ("of experience", "in DevOps", "as an engineer", "Experience: 3-5 Years", a bullet that starts with
it), and not education ("15 years full time education"), the company's age ("over 25 years of innovation",
"for more than 15 years", "3 years in a row") or anything above 20 years. The pick, in order: a labelled one
anywhere ("Experience: 3-5 Years", "Minimum 5 Year(s) Of Experience Is Required", "8+ years of relevant
experience."), then one in the title ("Python Developer 3-4 years Exp"), then the first in the requirements and
qualifications (`asks`), then the first in the whole description.

VERSION is stored with each job (raw.experience_parser); db.reparse_experience() re-reads saved jobs when it
changes, so bump it with every change here.
"""

import re

VERSION = 2
MAX_MIN = 20     # a minimum above this is company history, not a requirement

WORDS = {w: n for n, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUM = rf"(?:\d{{1,2}}(?:\.\d+)?|\b(?:{'|'.join(WORDS)}))(?:\s*\(\d{{1,2}}\))?"     # "5", "7.5", "five", "Five (5)"
UNIT = r"(?:years?|yrs?|year\(s\))(?:\b|(?<=\)))(?:'|’)?"
CANDIDATE = re.compile(
    rf"(?<![\d.])(?P<mlo>\d{{1,2}})\s*months?\s*(?:-|–|to)\s*(?P<mhi>{NUM})\s*{UNIT}"      # "6 months to 2 years"
    rf"|(?<![\d.])(?P<lo>{NUM})\s*(?:\+|plus|or more|or above)?\s*"
    rf"(?:(?:-|–|—|to)\s*(?P<hi>{NUM})\s*\+?\s*)?(?P<unit>{UNIT}|months?)",
    re.I)

# Words right after "N years" that make it something else
NOT_AFTER = re.compile(
    r"^\W*(?:of\s+)?(?:full[- ]?time\s+|formal\s+|regular\s+)?(?:education|schooling|study|studies)\b"
    r"|^\W*(?:in a row|since|ago|old\b|of age|warranty|guarantee|running|straight)"
    r"|^\W*(?:degree|contract|program|course|plan|term|bond|tenure|commitment|period)\b"
    r"|^\W*(?:old\s+)?(?:industry\s+)?(?:leader|company|firm|organi[sz]ation|brand|institution|heritage|legacy)\b"
    r"|^\W*of\s+(?:our|its|their|the company|history|innovation|ground|excellence|delivery excellence|legacy|"
    r"service to|trust|operations?\b|existence|journey|truly|growth|success|serving|partnership|business|"
    r"impact|transforming|helping|building trust|combined|collective)",
    re.I)
# ... and right before it ("for more than 15 years", "after almost 70 years", "the first 25 years")
NOT_BEFORE = re.compile(
    r"(?:\bfor|\bafter|\bfirst|\blast|\bpast|\bnext|\bwithin|\bevery|\bcelebrating|\bfounded|\bin less than|"
    r"\bin the nation for|\bspanning|\bnearly|\balmost|\bonce in|\badditional|\bor equivalent|\btime|\bby)"
    r"\s*(?:over|more than|nearly|almost|about|around|minimum|min)?\s*$",
    re.I)
# Experience words before it in the same sentence ("Experience: ", "Minimum", "at least") ...
EXP_BEFORE = re.compile(
    r"\b(?:experience[ds]?|exp|expertise|background|minimum|min|at\s?least|overall|total|required|requirement|"
    r"greater than|more than|bring|possess|typically)\b"
    # "Bachelor's degree with 6+ years", "High School Diploma with 6 years", "MS + ~3 years"
    r"|\b(?:degree|diploma|bachelor'?s|master'?s|ph\.?d|ms|bs|graduate)\b[^.]*?\b(?:with|and|plus)\W*$"
    r"|\b(?:degree|diploma|bachelor'?s|master'?s|ph\.?d|ms|bs)\s*\+\s*~?$", re.I)
# ... or after it ("of experience", "in DevOps", "as an engineer", "building ...", "hands-on ...")
EXP_AFTER = re.compile(
    r"^\W*(?:of\s+)?(?:[\w/&.+#-]+[\s,]+){0,5}?(?:experience|exp|expertise)\b"
    r"|^\W*(?:of|in|as|with|on|at|building|working|developing|designing|leading|managing|delivering|writing|"
    r"hands[- ]on|post|relevant|professional|industry|industrial|overall|total|non-internship|full[- ]?time|prior|"
    r"proven|practical|prac|technical|software|programming|related|direct|progressive|combined|work|experience|"
    r"exp|expertise|minimum|min|and above|or more|plus)\b",
    re.I)
# A label that starts the line, right before it: "Experience: ", "**Experience Range:** ", "Exp- min ",
# "Work experience required- ", "* **Experience:** Minimum of " (not "Hands-on experience (2+ years)")
SEP = r"[\s*:\-–_.]*"
LABEL_BEFORE = re.compile(
    rf"^(?:\*\*[^*\n]+\*\*\s*)*[\s*•·#>-]*(?:[\w'’-]+\s+){{0,3}}?(?:experience|exp)\b{SEP}"
    rf"(?:(?:range|required|requirement|level|needed|\(in years\)|in years){SEP})?"
    rf"(?:(?:min(?:imum)?|at\s?least|of){SEP}){{0,2}}$", re.I)
# ... or, for one that starts the line, the whole rest of it is "of (relevant) experience (is required)"
LABEL_AFTER = re.compile(
    r"^\W*(?:of\s+)?(?:(?:total|overall|relevant|work|professional|industry|it|hands-on|proven)\s+){0,2}"
    r"(?:experience|exp)\b\W*(?:(?:is\s+)?(?:required|needed|mandatory|preferred))?\W*$", re.I)
FRESHER = re.compile(r"\bfreshers?\b", re.I)
NOT_FRESHER = re.compile(r"\b(?:not|no|don'?t|do not|won'?t|will not|cannot|can'?t|aren'?t|isn'?t|unless)\b", re.I)


def number(text):
    text = re.sub(r"\s*\(\d+\)", "", text).strip().lower()
    return WORDS[text] if text in WORDS else int(float(text))


def sentence_before(text, start):
    """The words before `start` in its own line and sentence ("8+ yrs." doesn't end a sentence for what follows)."""
    line = text[text.rfind("\n", 0, start) + 1:start]
    cut = max(line.rfind(". "), line.rfind("; "), line.rfind("! "), line.rfind("? "))
    return line[cut + 2:] if cut >= 0 else line


def candidates(text):
    """(range, labelled, its text) for every "N years" in the text that is experience, in the order they appear."""
    for m in CANDIDATE.finditer(text or ""):
        before = sentence_before(text, m.start())
        end = text.find("\n", m.end())
        after = text[m.end():end if end >= 0 else len(text)]
        before_words = before.strip(" *-•·+–>#\t0123456789.)(")
        if NOT_AFTER.search(after) or NOT_BEFORE.search(before):
            continue
        if m["mlo"]:
            lo, hi = 0, number(m["mhi"])
        else:
            lo, hi = number(m["lo"]), number(m["hi"]) if m["hi"] else None
            if m["unit"].lower().startswith("month"):
                if hi is not None:       # "6-12 months"
                    lo, hi = lo // 12, max(hi // 12, 1)
                else:
                    lo, hi = lo // 12, None
            elif re.search(r"(?:\bup\s?to|<|\bless than|\bunder|\bmax(?:imum)?\.?)\W*$", before, re.I):
                lo, hi = 0, lo
        if lo > MAX_MIN or (hi is not None and hi < lo):
            continue
        if hi is not None and hi > 40:
            hi = None
        starts_line = not before_words or re.fullmatch(r"(?:min(?:imum)?|at\s?least)(?: of)?\W*", before_words, re.I)
        labelled = bool(LABEL_BEFORE.search(before) or (starts_line and LABEL_AFTER.search(after)))
        line_ends = not after.strip(" *.,;:)!_\t")
        if m["unit"] and m["unit"].lower().startswith("month"):     # not "training is 3 months", "by 6 months you'll"
            experience = labelled or re.match(r"\W*(?:of\s+)?(?:[\w-]+\s+){0,3}?(?:experience|exp)\b", after, re.I)
        else:
            experience = (labelled or starts_line or EXP_BEFORE.search(before) or EXP_AFTER.search(after)
                          or (line_ends and re.search(r"\b(?:with|having|need|needs|seeking)\b", before, re.I))
                          # "* Backend Development (3+ years)"
                          or (re.search(r"\(\s*$", before) and re.match(r"\s*\)", after)
                              and re.match(r"\s*[*•·-]", text[text.rfind("\n", 0, m.start()) + 1:])))
        if experience:
            yield {"min": lo, "max": hi}, labelled, text[m.start():m.end() + 30]


def title_years(title):
    """ "Python Developer 3-4 years Exp", "QA Engineer - 5+yrs": any "N years" in a title is experience."""
    for m in CANDIDATE.finditer(title or ""):
        if m["mlo"] or not m["unit"].lower().startswith("month"):
            lo = 0 if m["mlo"] else number(m["lo"])
            hi = number(m["mhi"] or m["hi"]) if (m["mhi"] or m["hi"]) else None
            if lo <= MAX_MIN and (hi is None or hi >= lo):
                return {"min": lo, "max": hi}
    return None


def years(title, asks, description):
    found = list(candidates(description))
    labelled = next((r for r, is_label, _ in found if is_label), None)
    # The requirements as split_sections() cut them out, matched back to the description, where the words around
    # each one are as written ("up to **1 year of experience**" became a heading line of its own there)
    in_asks = next((r for r, _, words in found if asks and words.strip() in asks), None)
    pick = labelled or title_years(title) or in_asks or (found[0][0] if found else None)
    if pick:
        # "5+ Yrs" in the title and "5 to 8 years of experience" in the text: 5 to 8
        if pick["max"] is None:
            pick = next((r for r, _, _ in found if r["min"] == pick["min"] and r["max"] is not None), pick)
        return pick
    for text in (title or "", description or ""):
        for m in FRESHER.finditer(text):
            if not NOT_FRESHER.search(sentence_before(text, m.start()) + text[m.end():m.end() + 40].split("\n")[0]):
                return {"min": 0, "max": 0}
    return None

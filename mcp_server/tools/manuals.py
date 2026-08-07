"""RAG over a small maintenance-manual corpus.

RETRIEVAL METHOD: lexical scoring (token overlap with IDF-style weighting), not
embeddings. That is a deliberate trade-off, not a shortcut:

  - It has no external dependency, no API key, and no network call, so the eval
    harness in Phase 5 can assert exact retrieval results in CI.
  - It is deterministic, so a failing eval means the code changed rather than a
    model drifted.
  - Over a corpus this small, recall is not the interesting problem; the
    interesting problem is the grounding contract — every answer carries the
    section id it came from, so a claim can be traced back to a source.

For a production corpus this would be a vector index. The interface below would
not change, which is the point of putting retrieval behind a tool.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    section_id: str
    title: str
    equipment: str
    text: str


CORPUS: tuple[Section, ...] = (
    Section(
        section_id="PMP-4.2",
        title="High bearing temperature on feedwater pumps",
        equipment="pump",
        text=(
            "A feedwater pump bearing temperature above 80 degC indicates degraded "
            "lubrication or misalignment. Reduce load to 60 percent and verify the "
            "lubrication oil level before continuing operation. If temperature "
            "exceeds 95 degC, shut the pump down immediately and isolate it. "
            "Do not restart until the bearing has been inspected."
        ),
    ),
    Section(
        section_id="PMP-4.5",
        title="Vibration limits for feedwater pumps",
        equipment="pump",
        text=(
            "Feedwater pump vibration must remain below 7.1 mm/s RMS during normal "
            "operation. Between 7.1 and 11.0 mm/s the pump may continue running but "
            "must be scheduled for alignment inspection within 72 hours. Above "
            "11.0 mm/s, shut down immediately: continued operation risks bearing "
            "and seal failure."
        ),
    ),
    Section(
        section_id="PMP-7.1",
        title="Feedwater pump restart procedure",
        equipment="pump",
        text=(
            "Before restarting a feedwater pump, confirm the suction valve is open, "
            "the discharge valve is throttled to 25 percent, and bearing temperature "
            "has fallen below 45 degC. Start the pump and ramp the discharge valve "
            "over no less than five minutes. Record the restart in the shift log."
        ),
    ),
    Section(
        section_id="CMP-2.3",
        title="Air compressor discharge temperature",
        equipment="compressor",
        text=(
            "Air compressor discharge temperature above 85 degC usually indicates "
            "a fouled intercooler or low coolant flow. Inspect the intercooler for "
            "fouling and verify coolant flow before returning the compressor to "
            "full load. Sustained operation above 100 degC will damage the valve "
            "plates."
        ),
    ),
    Section(
        section_id="CMP-5.1",
        title="Air compressor vibration and mounting",
        equipment="compressor",
        text=(
            "Compressor vibration above 7.1 mm/s commonly indicates loosened "
            "mounting bolts or a worn coupling. Torque the mounting bolts to "
            "specification and inspect the coupling element. Replace the coupling "
            "if elastomer cracking is visible."
        ),
    ),
    Section(
        section_id="GEN-1.1",
        title="Lockout tagout before maintenance",
        equipment="general",
        text=(
            "Before any intrusive maintenance, isolate the equipment from all "
            "energy sources, apply a personal lock and tag, and verify zero energy "
            "state. Only the person who applied a lock may remove it. This applies "
            "to every asset regardless of type."
        ),
    ),
)

_TOKEN = re.compile(r"[a-z0-9]+")

# Function words carry no topical signal, and IDF actively MISRANKS them in a
# corpus this small: "do" appears in exactly one section, so IDF scores it as
# highly informative and a query containing "how do I..." gets pulled toward
# whichever section happens to say "Do not". Found by the Phase 5 eval
# (retrieval-restart-procedure), which is precisely the class of bug unit tests
# do not catch — every individual function still worked.
_STOPWORDS = frozenset(
    """
    a an and are as at be been before but by can do does for from had has have
    how i if in into is it its may must no not of on or should so than that the
    their then there these they this to until up was were what when where which
    while who why will with would you your
    """.split()
)

#: A hit must match at least this fraction of the query's content words. One
#: incidental term overlap ("alignment" in an otherwise unrelated query) is not
#: a match, and returning it as one invites the model to ground an answer in an
#: irrelevant section — worse than returning nothing.
MIN_QUERY_COVERAGE = 0.34


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _content_tokens(text: str) -> set[str]:
    """Tokens that carry topical meaning — stopwords removed."""
    return {t for t in _tokenize(text) if t not in _STOPWORDS}


# Document frequency, computed once at import. Common words ("the", "pump")
# carry less signal than rare ones ("intercooler"), and without this weighting
# every query matches every section about pumps.
_DOC_FREQ: Counter[str] = Counter()
for _section in CORPUS:
    for _token in _content_tokens(f"{_section.title} {_section.text}"):
        _DOC_FREQ[_token] += 1


#: How much more a title match counts than a body match. A section titled
#: "Feedwater pump restart procedure" is *about* restarting; one that mentions
#: restarting in a closing sentence is not. Without this, the two are
#: indistinguishable and ranking falls back to alphabetical order — which is
#: exactly the bug the Phase 5 eval caught (retrieval-restart-procedure).
_TITLE_WEIGHT = 3.0


def _score(query_tokens: set[str], section: Section) -> tuple[float, float]:
    """Return (relevance, coverage) for one section.

    Coverage — the fraction of the query's content words this section matches —
    is tracked separately from relevance because they answer different
    questions. Relevance ranks the hits; coverage decides whether something is
    a hit at all. A section that matches one rare word scores respectably on
    relevance while covering almost none of what was asked.

    Relevance combines three signals, all of which the eval showed are needed:
      IDF          rare terms are more informative than common ones
      sub-linear TF  repeated mentions matter, but the tenth is not worth the
                     first — hence log, not a raw count
      title boost  what a section is titled beats what it mentions in passing
    """
    title_counts = Counter(
        t for t in _tokenize(section.title) if t not in _STOPWORDS
    )
    body_counts = Counter(t for t in _tokenize(section.text) if t not in _STOPWORDS)

    matched = query_tokens & (set(title_counts) | set(body_counts))
    if not matched:
        return 0.0, 0.0

    relevance = 0.0
    for token in matched:
        idf = math.log(len(CORPUS) / (1 + _DOC_FREQ[token])) + 1.0
        weighted_count = _TITLE_WEIGHT * title_counts[token] + body_counts[token]
        relevance += idf * (1.0 + math.log(weighted_count))

    return relevance, len(matched) / len(query_tokens)


def search_manuals(query: str, max_results: int = 3) -> dict:
    """Search the maintenance manuals and return matching sections.

    Every result carries its `section_id` so the model can cite it and a human
    can check it. Grounding without traceability is just a nicer-sounding guess.
    """
    tokens = _content_tokens(query)
    if not tokens:
        return {
            "query": query,
            "results": [],
            "note": "Query contained no searchable terms.",
        }

    scored = [(s, *_score(tokens, s)) for s in CORPUS]
    hits = sorted(
        [
            (s, relevance)
            for s, relevance, coverage in scored
            if relevance > 0 and coverage >= MIN_QUERY_COVERAGE
        ],
        key=lambda pair: (-pair[1], pair[0].section_id),
    )[: max(1, min(max_results, 10))]

    if not hits:
        return {
            "query": query,
            "results": [],
            "note": "No manual section matched. Do not guess — say so.",
        }

    return {
        "query": query,
        "results": [
            {
                "section_id": s.section_id,
                "title": s.title,
                "equipment": s.equipment,
                "text": s.text,
                "relevance": round(score, 3),
            }
            for s, score in hits
        ],
    }

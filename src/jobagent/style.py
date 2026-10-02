"""How the candidate writes. Every piece of free text the agent produces goes through this."""
from __future__ import annotations

import re

HUMAN_STYLE = """\
Write as the candidate: an engineer who ships things and writes the way they talk.
- Plain words, short sentences mixed with a few longer ones. First person. Contractions are fine.
- It's about THEM: open with the problem their product/team has (from the posting) and what the candidate would
  do about it in their first months. Every project mentioned must be tied to a specific value for them
  ("so I can ..." / "which is the same problem you have with ...").
- Don't make it about the current employer. Name it at most once, in passing, without describing its business;
  talk about what the candidate built and learned. Prefer naming the work itself ("a support bot answering
  2,000 tickets a day") over the employer.
- One specific detail about the company or role that shows the candidate actually read the posting.
- No greeting fluff, no summary of the job back to them, no closing pitch. Stop when the point is made.
- Never use these words or phrases: excited, thrilled, passionate, delve, leverage, utilize, synergy, robust,
  seamless, cutting-edge, innovative, dynamic, fast-paced, spearhead, tapestry, landscape, realm, testament,
  "I am writing to", "I'm confident", "proven track record", "aligns perfectly", "unique blend", "hit the ground
  running", "add value", "I believe I would be a great fit", "Furthermore", "Moreover", "In conclusion".
- No em dashes or en dashes; use commas, full stops or brackets. No bullet points, no bold, no emojis.
- Don't make every paragraph the same length or every sentence the same shape. Slightly informal is fine.
- Never invent facts. Only use what's in the profile and resume.
"""

_BANNED = re.compile(
    r"\b(excited|thrilled|passionate|delve|leverag\w*|utiliz\w*|synerg\w*|robust|seamless(ly)?|cutting-edge|"
    r"innovative|dynamic|fast-paced|spearhead\w*|tapestry|landscape|realm|testament|furthermore|moreover|"
    r"in conclusion|proven track record|aligns? perfectly|unique blend)\b", re.I)


def tells(text: str) -> list[str]:
    """AI-writing tells still present in text."""
    found = sorted({m.group(0).lower() for m in _BANNED.finditer(text)})
    if re.search(r"[—–]", text):
        found.append("em/en dash")
    return found


def clean(text: str) -> str:
    """Mechanical last pass: typographic tells a person typing in a form wouldn't produce."""
    text = re.sub(r"\s*[—–]\s*", ", ", text)          # em/en dashes -> comma
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("…", "...")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                  # stray markdown bold
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)     # bullet markers
    text = re.sub(r",\s*,", ",", text)
    return text.strip()


HUMAN_QUESTION = {
    "type": "score",
    "instructions": "Does this read like a real person wrote it quickly and sincerely, or like AI-generated / "
                    "templated text (stock phrases, perfectly parallel structure, generic enthusiasm)?",
    "criteria": ["obviously AI / template", "somewhat generic", "mostly natural", "clearly a real person's voice"],
}

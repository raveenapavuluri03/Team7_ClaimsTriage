"""Control 1: untrusted-content guard (plain code, runs BEFORE any model call).

The claim narrative is written by the customer, so it is untrusted. If it contains text that tries to
instruct the AI (instead of describing an accident), the claim is stopped at S1 and sent to a human.
The narrative never reaches the model, so nothing in it can change what the model extracts.
"""
import re
import unicodedata

MAX_NARRATIVE_CHARS = 4000

# zero-width and direction-control characters can hide text from a human reader
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# (rule name, pattern). Names are logged; the narrative text itself is not.
RULES = [
    ("override_instructions",
     r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(instructions?|rules?|prompts?|guidelines?)\b"),
    ("addresses_the_ai",
     r"\b(note|message|instruction|attention|important)s?\s+(to|for)\s+(the\s+)?"
     r"(ai|assistant|model|llm|system|automated\s+system|extraction\s+(system|tool))\b"),
    ("role_or_prompt_markers",
     r"(^|\n)\s*(system|assistant|developer)\s*:|<\s*/?\s*(system|assistant|instructions?)\s*>"
     r"|\[\s*/?\s*(inst|system)\s*\]|```"),
    ("persona_change",
     r"\byou\s+are\s+(now|no\s+longer)\b|\bpretend\s+(to\s+be|that)\b|\bact\s+as\s+(an?\s+)?(ai|assistant|system)\b"),
    ("dictates_output",
     r"\b(set|mark|record|output|return)\b[^.\n]{0,40}\b(police[_ ]report|incident[_ ]type|amount|risk|fraud|coverage)\b"
     r"[^.\n]{0,25}\b(as|to)\b\s*[\"']?(yes|no|true|false|low|covered|[0-9$])"),
]
_COMPILED = [(name, re.compile(p, re.IGNORECASE)) for name, p in RULES]


def scan_narrative(text):
    """Return a list of finding names. An empty list means the narrative may go to the model."""
    findings = []
    text = text if isinstance(text, str) else str(text)
    if len(text) > MAX_NARRATIVE_CHARS:
        findings.append("too_long")
    if _INVISIBLE.search(text):
        findings.append("hidden_characters")
    clean = unicodedata.normalize("NFKC", _INVISIBLE.sub("", text))     # fold look-alike characters
    for name, pattern in _COMPILED:
        if pattern.search(clean):
            findings.append(name)
    return findings
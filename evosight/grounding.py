"""Resolve quoted evidence to the archive's own spelling and fragment identifiers."""
import re

from .contracts import Citation


PUNCTUATION = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "\u00ad": ""})


def normalized_span(value):
    text, offsets = [], []
    for match in re.finditer(r"\s+|\S", value):
        char = " " if match.group().isspace() else match.group().translate(PUNCTUATION)
        if char:
            text.append(char)
            offsets.append(match.span())
    return "".join(text), offsets


def verbatim(source, quote):
    if len(quote.strip()) < 2:
        return None
    if quote in source:
        return quote
    # An outer ellipsis denotes an excerpt. Never join separate clauses across omitted text.
    query = re.sub(r"^(?:…+|\.{3,})\s*|\s*(?:…+|\.{3,})$", "", quote).strip()
    if len(query) < 2:
        return None
    if query in source:
        return query
    needle, _ = normalized_span(query)
    if len(needle.strip()) < 2:
        return None
    haystack, offsets = normalized_span(source)
    at = haystack.find(needle)
    if at < 0:
        return None
    return source[offsets[at][0] : offsets[at + len(needle) - 1][1]]


def locate(cite, visible, *, relocate=True):
    origin = visible.get(cite.fragment_id)
    if origin:
        exact = verbatim(origin["text"], cite.quote)
        if exact:
            return Citation(fragment_id=cite.fragment_id, quote=exact)
        if relocate and origin.get("document_id") is not None:
            hits = [
                (key, match)
                for key, fragment in visible.items()
                if key != cite.fragment_id
                and fragment.get("document_id") == origin["document_id"]
                and (match := verbatim(fragment["text"], cite.quote))
            ]
            if len(hits) == 1:
                return Citation(fragment_id=hits[0][0], quote=hits[0][1])
    raise ValueError("引文无法在所引文档已给出的片段中唯一定位")


def field_path(key):
    name = key.strip().removeprefix("$").lstrip("./").replace("/", ".")
    name = re.sub(r"""\[['"]?([^\]'"]+)['"]?\]""", r".\1", name)
    return name.lstrip(".")


def field_name(key, finding, labels):
    name = field_path(key)
    short = name.removeprefix("attributes.")
    if short in finding.attributes:
        return "attributes." + short
    for field, label in labels.items():
        if short == label and field in finding.attributes:
            return "attributes." + field
    if name in finding.facets:
        return "facets." + name
    if name in ("subject", "text", "occurred_at") or (
        name.startswith("facets.") and name.removeprefix("facets.") in finding.facets
    ):
        return name
    raise ValueError("字段依据指向本项不存在的字段")

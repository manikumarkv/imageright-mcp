"""Natural-language search over catalog operations.

A small weighted keyword ranker: each operation is split into fields (name, summary, path,
capability, area, explanation, params, gotchas). Query words are stemmed, expanded with the
synonym table below, and matched against those fields, weighted by field and by how rare the
word is. Words that match nothing get one fuzzy retry, which catches simple typos. Operations whose
name the query mostly covers get a small bonus.
Quality is gated by the eval suite in ``tests/eval/cases.json``.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_STOPWORD_TEXT = (
    "a an and are as at be by can do does for from how i if in into is it its me my of on "
    "or so that the their them then there this to up via what when where which whom "
    "with you your want need should would could api call endpoint operation"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

# Query word -> related catalog words. Keys and values are stemmed at import time.
_RAW_SYNONYMS: dict[str, tuple[str, ...]] = {
    "upload": ("create", "add", "insert", "page", "image"),
    "attach": ("add", "create", "page"),
    "add": ("create", "insert"),
    "new": ("create",),
    "make": ("create",),
    "insert": ("add", "create"),
    "remove": ("delete", "kill"),
    "erase": ("delete",),
    "trash": ("delete",),
    "fetch": ("get",),
    "retrieve": ("get",),
    "read": ("get",),
    "show": ("get",),
    "view": ("get",),
    "open": ("get",),
    "list": ("get", "all"),
    "browse": ("get", "list"),
    "search": ("find",),
    "lookup": ("find", "get"),
    "locate": ("find",),
    "query": ("find",),
    "look": ("find", "get"),
    "transfer": ("move",),
    "relocate": ("move",),
    "assign": ("user", "assign", "step"),
    "assignee": ("user", "assign"),
    "forward": ("route", "release"),
    "send": ("route", "release"),
    "advance": ("route", "release"),
    "complete": ("release",),
    "finish": ("release",),
    "done": ("release",),
    "cancel": ("kill",),
    "abort": ("kill",),
    "terminate": ("kill",),
    "stuck": ("error", "history", "release"),
    "fail": ("error", "history"),
    "failure": ("error", "history"),
    "broken": ("error",),
    "why": ("history",),
    "diary": ("task", "diary"),
    "workitem": ("task",),
    "job": ("task",),
    "queue": ("task", "step"),
    "inbox": ("task", "current"),
    "doc": ("document",),
    "image": ("page", "image"),
    "scan": ("page", "image"),
    "picture": ("image", "page"),
    "tif": ("image", "page"),
    "tiff": ("image", "page"),
    "pdf": ("image", "page"),
    "policy": ("file",),
    "claim": ("file",),
    "cabinet": ("drawer",),
    "login": ("authenticate", "login"),
    "logon": ("authenticate", "login"),
    "signin": ("authenticate", "login"),
    "sign": ("authenticate", "login"),
    "token": ("authenticate", "valid"),
    "expire": ("valid",),
    "expiry": ("valid",),
    "logout": ("logoff",),
    "checkout": ("lock",),
    "checkin": ("unlock",),
    "reserve": ("lock",),
    "permission": ("permission", "right", "access"),
    "right": ("permission",),
    "access": ("permission",),
    "allow": ("permission", "allowed"),
    "account": ("user", "account"),
    "person": ("user",),
    "people": ("user",),
    "comment": ("note",),
    "flag": ("mark",),
    "tag": ("mark",),
    "field": ("attribute",),
    "property": ("attribute", "property"),
    "metadata": ("attribute", "property"),
    "index": ("attribute",),
    "template": ("type",),
    "kind": ("type",),
    "flow": ("workflow",),
    "process": ("workflow",),
    "stage": ("step",),
    "audit": ("history",),
    "log": ("history",),
    "download": ("stream", "get"),
    "export": ("image", "stream"),
    "combine": ("merge",),
    "join": ("merge",),
    "duplicate": ("copy",),
    "rename": ("update", "description"),
    "edit": ("update",),
    "change": ("update", "set"),
    "modify": ("update",),
    "set": ("update",),
    "count": ("count",),
    "health": ("health",),
    "alive": ("health",),
    "ping": ("health",),
    "version": ("version",),
    "who": ("user",),
    "am": ("current",),
    "whoami": ("current", "user"),
    "online": ("health",),
    "reachable": ("health",),
    "mail": ("email",),
    "mailbox": ("email", "account"),
    "deadline": ("sla",),
    "overdue": ("sla",),
    "undelete": ("restore",),
    "recover": ("restore",),
    "priority": ("priority",),
}

FIELD_WEIGHTS: dict[str, float] = {
    "name": 3.5,
    "summary": 2.5,
    "capability": 2.0,
    "path": 2.0,
    "area": 1.0,
    "explanation": 1.0,
    "params": 0.5,
    "gotchas": 0.5,
    "response": 0.5,
}
SYNONYM_WEIGHT = 0.6
FUZZY_WEIGHT = 0.8
# Bonus for operations whose name is mostly covered by the query ("list workflows" ->
# getWorkflows rather than getSteps): a tie-breaker for specific names.
NAME_COVERAGE_WEIGHT = 0.5


def split_words(text: str) -> list[str]:
    """Split on non-alphanumerics and camelCase boundaries, lowercased."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    return re.findall(r"[a-z0-9]+", spaced.lower())


def stem(word: str) -> str:
    """Tiny suffix stripper; applied identically to catalog text and queries.

    It only has to be consistent, not linguistic: "create", "created" and "creating" all
    become "creat".
    """
    if len(word) <= 3:
        return word
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)] + repl
            word = word[:-1] if suffix == "es" and not base.endswith(("s", "x")) else base
            break
    else:
        if word.endswith("s") and not word.endswith(("ss", "us")):
            word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeioulsz":
        word = word[:-1]  # "logged" -> "logg" -> "log"
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [stem(w) for w in split_words(text) if w not in STOPWORDS]


SYNONYMS: dict[str, tuple[str, ...]] = {
    stem(key): tuple(stem(v) for v in values) for key, values in _RAW_SYNONYMS.items()
}


def _texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


@dataclass
class _Doc:
    op_id: str
    fields: dict[str, set[str]]
    prior: float
    all_terms: set[str] = field(default_factory=set)


def _document(op: dict[str, Any], area: str, response: str) -> _Doc:
    if op["surface"] == "soap":
        name = str(op["operation"])
        path = ""
    else:
        name = str(op["id"]).rsplit(".", 1)[1]
        path = " ".join(
            seg
            for seg in str(op["path"]).split("/")
            if seg and not seg.startswith("{") and seg not in {"api", "v2"}
        )
    params = [str(p["name"]) for p in op["params"] if not p.get("token")]
    params += [str(p.get("meaning", "")) for p in op["params"]]
    raw = {
        "name": name,
        "summary": str(op["summary"]),
        "capability": str(op.get("capability") or ""),
        "path": path,
        "area": area,
        "explanation": str(op.get("explanation") or ""),
        "params": " ".join(params),
        "gotchas": " ".join(_texts(op.get("gotchas"))),
        "response": response,
    }
    fields = {key: set(tokens(text)) for key, text in raw.items()}
    prior = 1.0
    if op.get("annotated"):
        prior *= 1.15
    if op.get("capability"):
        prior *= 1.1
    doc = _Doc(op_id=str(op["id"]), fields=fields, prior=prior)
    doc.all_terms = set().union(*fields.values())
    return doc


class SearchIndex:
    def __init__(
        self,
        ops: Iterable[dict[str, Any]],
        area_of: dict[str, str],
        response_text: dict[str, str] | None = None,
    ) -> None:
        responses = response_text or {}
        self._docs = [
            _document(op, area_of[str(op["id"])], responses.get(str(op["id"]), "")) for op in ops
        ]
        df: dict[str, int] = {}
        for doc in self._docs:
            for term in doc.all_terms:
                df[term] = df.get(term, 0) + 1
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._vocab = sorted(df)

    def _variants(self, term: str) -> list[tuple[str, float, float]]:
        """``(catalog term, weight, idf)`` alternatives for one query term.

        A synonym match stands for the query word, so it is at least as informative as that
        word: "upload" -> "create" should not count as little as the very common "create".
        Unknown words get one fuzzy retry, which catches simple typos ("uplaod").
        """
        if term not in self._idf and term not in SYNONYMS:
            close = difflib.get_close_matches(term, self._vocab, n=1, cutoff=0.8)
            if not close:
                return []
            return [(t, w * FUZZY_WEIGHT, i) for t, w, i in self._variants(close[0])]
        own = self._idf.get(term, 0.0)
        variants = [(term, 1.0, own)] if term in self._idf else []
        variants += [
            (s, SYNONYM_WEIGHT, max(self._idf[s], own))
            for s in SYNONYMS.get(term, ())
            if s != term and s in self._idf
        ]
        return variants

    def rank(self, query: str, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """Return ``(operationId, score)`` pairs, best first; empty when nothing matches."""
        terms = list(dict.fromkeys(tokens(query)))
        if not terms:
            return []
        expanded = [self._variants(t) for t in terms]
        results: list[tuple[str, float]] = []
        for doc in self._docs:
            if allowed is not None and doc.op_id not in allowed:
                continue
            total = 0.0
            matched = 0
            name_hits: set[str] = set()
            for variants in expanded:
                best = 0.0
                for term, weight, idf in variants:
                    if term not in doc.all_terms:
                        continue
                    fw = max(FIELD_WEIGHTS[k] for k, v in doc.fields.items() if term in v)
                    best = max(best, weight * fw * idf)
                    if term in doc.fields["name"]:
                        name_hits.add(term)
                if best > 0:
                    matched += 1
                    total += best
            if matched == 0:
                continue
            coverage = matched / len(terms)
            name_coverage = len(name_hits) / max(1, len(doc.fields["name"]))
            score = total * (0.4 + 0.6 * coverage) * doc.prior
            results.append((doc.op_id, score * (1 + NAME_COVERAGE_WEIGHT * name_coverage)))
        results.sort(key=lambda r: (-r[1], r[0]))
        return results

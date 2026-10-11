"""Deterministic rules: the baseline every model decision is measured against.

Rules only claim what they are sure of. Anything ambiguous is left unresolved
(target=None) so the decision provider or a human settles it.
"""

import re
from dataclasses import dataclass

from app.mapping.target_schema import BY_NAME, TARGET_PROPERTIES

STRIP_PREFIXES = {"cust", "customer", "client", "contact", "primary", "main", "c", "user", "person", "lead"}
ABBREVIATIONS = {
    "acct": "account", "addr": "address", "num": "number", "nbr": "number", "no": "number",
    "ph": "phone", "tel": "telephone", "org": "organization", "co": "company", "desig": "designation",
}

# A strong keyword anywhere in the name points to one property (checked against the value type).
KEYWORDS: dict[str, str] = {
    "email": "email", "mail": "email", "emails": "email",
    "phone": "phone", "telephone": "phone", "tel": "phone", "landline": "phone",
    "mobile": "mobilephone", "cell": "mobilephone", "cellphone": "mobilephone",
    "surname": "lastname", "lastname": "lastname",
    "firstname": "firstname", "forename": "firstname",
    "company": "company", "employer": "company", "organization": "company", "organisation": "company",
    "website": "website", "url": "website", "homepage": "website",
    "zip": "zip", "zipcode": "zip", "postcode": "zip", "postal": "zip",
    "city": "city", "town": "city",
    "country": "country",
    "state": "state", "province": "state",
    "jobtitle": "jobtitle", "designation": "jobtitle", "position": "jobtitle",
    "lifecycle": "lifecyclestage",
}

# Which inferred value types are compatible with each target type.
TYPE_COMPAT: dict[str, set[str]] = {
    "email": {"email", "empty"},
    "phone": {"phone", "integer", "string", "empty"},
    "url": {"url", "string", "empty"},
    "string": {"string", "integer", "empty"},  # zip codes and IDs often look like integers
    "enum": {"string", "boolean", "empty"},
}

CONF_EXACT = 0.97
CONF_KEYWORD = 0.85
CONF_VALUE_PATTERN = {"email": ("email", 0.82), "url": ("website", 0.80), "phone": ("phone", 0.70)}


@dataclass
class RuleResult:
    column: str
    target: str | None
    confidence: float
    reason: str


def tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name.strip())
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_variants(name: str) -> set[str]:
    """Compact spellings of a column name: raw, prefix-stripped, abbreviation-expanded."""
    toks = tokens(name)
    variants = {compact(name), "".join(toks)}
    stripped = toks[1:] if len(toks) > 1 and toks[0] in STRIP_PREFIXES else toks
    expanded = [ABBREVIATIONS.get(t, t) for t in stripped]
    variants |= {"".join(stripped), "".join(expanded), "".join(ABBREVIATIONS.get(t, t) for t in toks)}
    return {v for v in variants if v}


_SYNONYM_INDEX: dict[str, str] = {}
for _prop in TARGET_PROPERTIES:
    for _syn in (_prop.name, _prop.label, *_prop.synonyms):
        _SYNONYM_INDEX.setdefault(compact(_syn), _prop.name)


def type_ok(target: str, inferred_type: str) -> bool:
    return inferred_type in TYPE_COMPAT.get(BY_NAME[target].type, {inferred_type})


def map_column(name: str, inferred_type: str) -> RuleResult:
    # 1) exact synonym match on any spelling of the name
    for variant in sorted(name_variants(name)):
        target = _SYNONYM_INDEX.get(variant)
        if target and type_ok(target, inferred_type):
            return RuleResult(name, target, CONF_EXACT, f"name '{name}' is a known synonym of {target}")

    # 2) a strong keyword inside the name, if the values agree
    toks = tokens(name)
    expanded = [ABBREVIATIONS.get(t, t) for t in toks]
    hits = {KEYWORDS[t] for t in toks + expanded + ["".join(toks)] if t in KEYWORDS}
    if len(hits) == 1:
        target = hits.pop()
        if type_ok(target, inferred_type):
            return RuleResult(name, target, CONF_KEYWORD, f"name contains a keyword for {target}")
        return RuleResult(name, None, 0.0,
                          f"name suggests {target} but values look like {inferred_type}; needs review")

    # 3) values have an unmistakable pattern
    if inferred_type in CONF_VALUE_PATTERN:
        target, conf = CONF_VALUE_PATTERN[inferred_type]
        return RuleResult(name, target, conf, f"values look like {inferred_type}")

    return RuleResult(name, None, 0.0, "no rule matched")


def resolve_conflicts(results: list[RuleResult]) -> list[RuleResult]:
    """Two columns can't both fill one property: keep the most confident, demote the rest."""
    best: dict[str, RuleResult] = {}
    for r in results:
        if r.target and (r.target not in best or r.confidence > best[r.target].confidence):
            best[r.target] = r
    out = []
    for r in results:
        if r.target and best[r.target] is not r:
            out.append(RuleResult(r.column, None, 0.0,
                                  f"also matched {r.target}, already taken by '{best[r.target].column}'"))
        else:
            out.append(r)
    return out


def normalize_value(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-/]+", " ", value.strip().lower())).strip()


def map_enum_value(target: str, value: str) -> tuple[str | None, float, str]:
    """Map one source value onto a target enum option, deterministically if possible."""
    prop = BY_NAME[target]
    norm = normalize_value(value)
    if norm in prop.options:
        return norm, CONF_EXACT, "exact option"
    if norm in prop.option_synonyms:
        return prop.option_synonyms[norm], CONF_EXACT, "known synonym"
    no_digits = re.sub(r"\s*\d+\s*", " ", norm).strip()
    if no_digits and no_digits != norm and no_digits in prop.option_synonyms:
        return prop.option_synonyms[no_digits], 0.90, "known synonym after removing numbers"
    return None, 0.0, "no rule matched"

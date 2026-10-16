"""Lightweight query vocabulary and identifier decomposition for code search."""
import re


_ALIASES = {
    "auth": ("authentication", "authenticate", "login", "signin", "identity"),
    "authentication": ("auth", "authenticate", "login", "signin", "identity"),
    "authenticate": ("auth", "authentication", "login", "signin"),
    "login": ("auth", "authentication", "signin", "sign_in", "authenticate"),
    "logout": ("signout", "sign_out", "session"),
    "signin": ("sign_in", "login", "authenticate"),
    "signout": ("sign_out", "logout", "session"),
    "jwt": ("token", "bearer", "credential"),
    "token": ("jwt", "bearer", "credential"),
    "credential": ("token", "password", "identity"),
    "password": ("credential", "reset", "authentication"),
    "mfa": ("multifactor", "two_factor", "verification"),
    "multifactor": ("mfa", "two_factor", "verification"),
    "verify": ("validate", "check", "authenticate"),
    "validate": ("verify", "check"),
    "remove": ("delete", "destroy"),
    "delete": ("remove", "destroy"),
    "update": ("modify", "change", "edit"),
    "modify": ("update", "change", "edit"),
    "create": ("add", "insert", "register"),
    "database": ("db", "storage", "repository"),
    "db": ("database", "storage", "repository"),
}

def identifier_tokens(text: str) -> list[str]:
    """Split snake_case, kebab-case, paths, and camelCase into searchable terms."""
    terms = []
    for segment in re.findall(r"[A-Za-z0-9]+", text):
        parts = re.findall(
            r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+",
            segment,
        )
        for part in parts:
            token = part.casefold()
            if len(token) > 1 and token not in terms:
                terms.append(token)
    return terms


def expand_query_terms(terms: list[str]) -> list[str]:
    """Expand query vocabulary with known aliases and identifier components."""
    base_terms = []
    for term in terms:
        candidates = [term, *identifier_tokens(term)]
        normalized = [candidate.casefold() for candidate in candidates]
        for candidate in normalized:
            if candidate not in base_terms:
                base_terms.append(candidate)

    expanded = list(base_terms)
    for candidate in base_terms:
        for canonical, aliases in _ALIASES.items():
            if candidate == canonical or candidate in aliases:
                for related in (canonical, *aliases):
                    if related not in expanded:
                        expanded.append(related)
    return expanded


def searchable_terms(text: str) -> str:
    """Build an index field from code vocabulary and its common aliases."""
    return " ".join(expand_query_terms(identifier_tokens(text)))

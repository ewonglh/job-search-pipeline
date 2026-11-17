"""Private, offline company classifications for cached analytics.

Catalog JSON belongs in the user's runtime state directory, never the checkout.
No company classification is guessed from a name; unresolved aliases stay unknown.
"""
import json
import os
import re
import tempfile
from urllib.parse import urlsplit

from config import default_runtime_path, path_setting


MAX_RECORDS = 5000
MAX_ALIASES = 40
SIZES = {"SME", "large", "unknown"}
FOOTPRINTS = {"domestic", "multinational", "unknown"}
CONFIDENCE = {"high", "medium", "low", "unknown"}
REVIEW = {"reviewed", "needs_review", "unknown"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_PRIVATE_RE = re.compile(r"(?i)(?:[\w.+-]+@[\w.-]+\.[A-Z]{2,}|(?:token|password|secret|api[_ -]?key)\s*[:=])")


class CatalogError(ValueError):
    """Safe catalog validation or persistence error."""


def default_path():
    return path_setting("JOBSEARCH_COMPANY_CATALOG", default_runtime_path("company_catalog.json"))


def _text(value, limit=160):
    if not isinstance(value, str):
        raise CatalogError("catalog text field is invalid")
    value = " ".join(value.split())
    if (not value or len(value) > limit or any(ord(c) < 32 for c in value)
            or _PRIVATE_RE.search(value)):
        raise CatalogError("catalog text field is unsafe")
    return value


def _alias(value):
    alias = " ".join(_text(value, 120).casefold().split())
    if "/" in alias or "\\" in alias or re.match(r"^[a-z]:", alias):
        raise CatalogError("catalog alias is unsafe")
    return alias


def _url(value):
    if value is None:
        return None
    value = _text(value, 2048)
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise CatalogError("catalog source URL is invalid")
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(c in value for c in "\\\r\n\t")):
        raise CatalogError("catalog source URL is unsafe")
    return value


def _record(value):
    if not isinstance(value, dict):
        raise CatalogError("catalog record must be an object")
    key = value.get("key")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise CatalogError("catalog key is invalid")
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        raise CatalogError("catalog aliases are invalid")
    aliases = sorted({_alias(item) for item in aliases})
    size = value.get("size", "unknown")
    footprint = value.get("footprint", "unknown")
    confidence = value.get("confidence", "unknown")
    review = value.get("review", "unknown")
    industry = value.get("primary_industry", "unknown")
    if size not in SIZES or footprint not in FOOTPRINTS:
        raise CatalogError("catalog classification is invalid")
    if confidence not in CONFIDENCE or review not in REVIEW:
        raise CatalogError("catalog review status is invalid")
    industry = _text(industry, 100)
    classified = value.get("classification_date")
    if classified is not None:
        if not isinstance(classified, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", classified):
            raise CatalogError("catalog classification date is invalid")
        try:
            from datetime import date
            if date.fromisoformat(classified).isoformat() != classified:
                raise ValueError()
        except ValueError:
            raise CatalogError("catalog classification date is invalid")
    return {"key": key, "aliases": aliases, "size": size,
            "footprint": footprint, "primary_industry": industry,
            "source_url": _url(value.get("source_url")),
            "classification_date": classified, "confidence": confidence,
            "review": review}


def load(path=None):
    """Load a bounded catalog. A missing file is a safe empty catalog."""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle, object_pairs_hook=_unique_pairs)
    except FileNotFoundError:
        return {"schema_version": 1, "records": []}
    except (OSError, UnicodeError, ValueError, TypeError):
        raise CatalogError("company catalog is unavailable or invalid")
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CatalogError("company catalog schema is unsupported")
    records = document.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise CatalogError("company catalog size is invalid")
    parsed = [_record(item) for item in records]
    if len({item["key"] for item in parsed}) != len(parsed):
        raise CatalogError("company catalog keys must be unique")
    return {"schema_version": 1, "records": parsed}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def upsert(record, path=None):
    """Explicitly insert or replace a record, then atomically persist it."""
    path = path or default_path()
    catalog = load(path)
    records = {item["key"]: item for item in catalog["records"]}
    if not isinstance(record, dict):
        raise CatalogError("catalog record must be an object")
    key = record.get("key")
    prior = records.get(key, {}) if isinstance(key, str) else {}
    merged = dict(prior)
    merged.update(record)
    clean = _record(merged)
    records[clean["key"]] = clean
    if len(records) > MAX_RECORDS:
        raise CatalogError("company catalog is full")
    # Preserve alias ambiguity; do not silently reassign another company's alias.
    catalog = {"schema_version": 1,
               "records": [records[key] for key in sorted(records)]}
    _write(path, catalog)
    return catalog


def resolve(catalog, company_name):
    """Resolve an exact normalized alias; ambiguous and absent names are unknown."""
    if not isinstance(company_name, str) or not company_name.strip():
        return None
    try:
        alias = _alias(company_name)
    except CatalogError:
        return None
    matches = [record for record in catalog.get("records", [])
               if alias in record.get("aliases", [])]
    return matches[0] if len(matches) == 1 else None


def _write(path, document):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".company-catalog-", dir=directory)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

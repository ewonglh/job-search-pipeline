"""Small stdlib helpers for neutral repo and runtime paths."""

import math
import os
import re


def _setting(name, default):
    value = os.environ.get(name)
    if value is None:
        value = default
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SystemExit("configuration error: %s must be a non-empty value" % name)
    return value.strip()


def path_setting(name, default):
    """Return an absolute path and reject explicitly empty or malformed values."""
    value = os.environ.get(name)
    if value is None:
        value = os.fspath(default)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SystemExit("configuration error: %s must be a non-empty path" % name)
    try:
        return os.path.abspath(os.path.expanduser(value))
    except (OSError, ValueError) as exc:
        raise SystemExit("configuration error: invalid %s path: %s" % (name, exc))


def value_setting(name, default):
    """Return a non-empty string setting, rejecting empty overrides."""
    return _setting(name, default)


def github_repo_setting(name, default):
    """Return a GitHub owner/repository slug or fail with a useful message."""
    value = _setting(name, default)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise SystemExit("configuration error: %s must be owner/repository" % name)
    return value


def int_setting(name, default):
    value = _setting(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise SystemExit("configuration error: %s must be an integer" % name)


def float_setting(name, default):
    value = _setting(name, str(default))
    try:
        number = float(value)
    except ValueError:
        raise SystemExit("configuration error: %s must be a number" % name)
    if not math.isfinite(number) or number < 0:
        raise SystemExit("configuration error: %s must be finite and non-negative" % name)
    return number


def default_repo_dir():
    return os.path.join(os.path.expanduser("~"), ".local", "share",
                        "job-search-pipeline", "resume")


def default_runtime_path(filename):
    return os.path.join(os.path.expanduser("~"), ".local", "state",
                        "job-search-pipeline", filename)

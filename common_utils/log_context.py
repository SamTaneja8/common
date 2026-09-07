from __future__ import annotations

import contextvars
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


LOG_FORMAT = (
    "%(asctime)s %(levelname)s [run=%(run_uuid)s]%(context_fields)s "
    "[%(threadName)s] %(name)s: %(message)s"
)


def resolve_log_dir(*, log_home: str | None, project_name: str | None, default_dir: Path) -> Path:
    """Resolves the directory a repo's log files should be written to, given
    its LOG_HOME/PROJECT_NAME settings.

    - LOG_HOME unset/empty: use default_dir (each repo's own local logs/,
      which is what docker-compose.yml actually volume-mounts).
    - LOG_HOME set: used as-is, with project_name appended as a
      subdirectory if set. A *relative* LOG_HOME is intentionally left
      relative rather than manually anchored anywhere -- ordinary
      relative-path semantics (relative to the process's CWD) are what
      whoever set LOG_HOME=./something would expect. A previous version of
      this logic (each repo had its own copy) instead force-anchored a
      relative LOG_HOME at the filesystem root, so LOG_HOME=./ silently
      resolved to "/" + project_name -- outside any mounted volume, so
      every run's log was written and then permanently discarded the
      moment its --rm container exited. That's a much worse failure mode
      than "wrote to an unexpected but discoverable relative path."
    """
    if not log_home:
        return default_dir
    base = Path(log_home).expanduser()
    if project_name:
        base = base / project_name
    return base

_run_uuid_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_uuid", default="")
_context_fields_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "context_fields", default={}
)


def _resolve_run_uuid() -> str:
    current = _run_uuid_var.get()
    if current:
        return current
    resolved = os.getenv("JOB_RUN_ID", "").strip() or str(uuid.uuid4())
    _run_uuid_var.set(resolved)
    return resolved


def push_log_context(**fields: Any) -> contextvars.Token:
    """Non-context-manager variant of bind_log_context for callers that can't
    use a single `with` block (e.g. a web framework's before_request/
    teardown_request hooks). Pair with pop_log_context(token) -- typically in
    a teardown/finally hook -- to avoid leaking fields into later requests on
    a reused thread."""
    merged = dict(_context_fields_var.get())
    merged.update({key: value for key, value in fields.items() if value is not None})
    return _context_fields_var.set(merged)


def pop_log_context(token: contextvars.Token) -> None:
    _context_fields_var.reset(token)


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Attach extra fields (e.g. asin=..., content_id=...) to every log line
    emitted within this block. Nested calls merge with the enclosing context
    and are restored on exit."""
    token = push_log_context(**fields)
    try:
        yield
    finally:
        pop_log_context(token)


class ContextFilter(logging.Filter):
    """Stamps run_uuid and any bind_log_context() fields onto every record so
    LOG_FORMAT never KeyErrors, whether or not a context is currently bound."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_uuid = _resolve_run_uuid()
        fields = _context_fields_var.get()
        record.context_fields = (
            " [" + " ".join(f"{key}={value}" for key, value in fields.items()) + "]"
            if fields
            else ""
        )
        return True


def install_context_filter(logger: logging.Logger | None = None) -> None:
    """Attaches the filter to the logger's HANDLERS, not the logger itself.
    A filter added via Logger.addFilter() only runs for records that
    originate on that exact logger object -- records from a child logger
    (the normal logging.getLogger(__name__) pattern) reach this logger's
    handlers via propagation without ever passing through this logger's own
    filter, so run_uuid/context_fields would never get set on them and
    LOG_FORMAT's %(run_uuid)s would KeyError for almost every real call
    site. Handler.filter() runs for every record the handler receives
    regardless of origin, so that's where this has to live."""
    target = logger or logging.getLogger()
    context_filter = ContextFilter()
    for handler in target.handlers:
        # Idempotent: the same handler object is commonly shared across
        # several loggers (e.g. reviewgate reassigns gunicorn's handlers
        # onto multiple named loggers), so this can be called once per
        # logger without stacking duplicate filters on a shared handler.
        if not any(isinstance(existing, ContextFilter) for existing in handler.filters):
            handler.addFilter(context_filter)

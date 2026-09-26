from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Sequence

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 512
_TIMEOUT_SECONDS = 30.0


def embed_text(
    text: str,
    *,
    api_key: str,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
) -> list[float]:
    """Returns an embedding vector for `text` via OpenAI's embeddings API.

    Embeddings always go through OpenAI regardless of a repo's own
    CONTENT_AI_PROVIDER/AI_ENGINE choice for text generation -- DeepSeek and
    Claude have no embeddings endpoint reachable through those same client
    setups, so this is intentionally provider-independent. Callers own
    retries and env-var reading (matching how build_deal_progress_embed in
    this module is a pure builder while each repo's own discord_alerts.py
    wraps the actual send with retry) -- this function raises on any
    non-2xx response or transport error rather than swallowing it.

    Uses stdlib urllib rather than httpx/requests: common_utils declares no
    dependencies of its own (see pyproject.toml) and is installed as-is into
    both reviewgate (which has requests+litellm but not httpx) and amazonnew
    (which has httpx but not requests) -- picking either would mean adding a
    new dependency to whichever repo doesn't already have it.
    """
    request = urllib.request.Request(
        OPENAI_EMBEDDINGS_URL,
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps({"model": model, "input": text, "dimensions": dimensions}).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI embeddings request failed ({exc.code}): {detail}") from exc
    return payload["data"][0]["embedding"]


def vector_literal(values: Sequence[float]) -> str:
    """Formats an embedding as a pgvector input literal (e.g. "[0.1,0.2]")
    for use with a `%s::vector` placeholder in raw SQL -- avoids needing the
    separate `pgvector` Python package (which registers a psycopg adapter)
    just to write one column."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"

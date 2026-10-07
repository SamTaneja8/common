"""Publishing to Dealvant: the shared write path used by amazonnew's publish
pipeline, reviewgate's publish and /roundups pages, and autopub's blogger.

- store: upserts for products, offers, articles and roundups, and the
  schema-version check (require_schema).
- products: Amazon page scan -> product record, US price parsing, and the
  foreign-price rule (also used by amazonnew's scanner and publish guard).
- roundups: roundup prompt, safe link assembly, pick labels, JSON-LD.
- checks: SEO/grammar/spelling checks shown to reviewers and used as the
  auto-publish gate (needs pyspellchecker in the consumer).

Lives under common_utils so it ships with the existing common_utils copy in
every consumer's image; no build changes needed.
"""

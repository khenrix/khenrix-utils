# Ecosystem discovery — evidence gate

Subjects: the engine's findings.json lists every loaded plugin + MCP server.
Cache: docs/setup-audit/ecosystem-cache.json {subject: {checked, verdict, citations}};
serve entries younger than 30 days; refresh on user request or expiry.

Order of evidence (stop at the first that answers):
1. Registry facts: GitHub releases / npm registry — last release date, archived flag.
2. Vendor docs / changelogs — deprecation or supersession notices.
3. Marketplace manifests — official vs community publisher.
4. Web search — only for "is there a better X", never as the sole citation.

A replacement recommendation REQUIRES all of: overlapping tool surface (named
tools), maintenance signal (release within 12 months or explicit LTS), publisher
status, and a migration cost note. Additions may be suggested ONLY from gaps the
user stated or the report demonstrates (never from mining session history).
No citation → no finding. Web failure → DISCOVERY INCOMPLETE (never "no
replacements exist"). Everything here is ADVISORY — no auto-apply.

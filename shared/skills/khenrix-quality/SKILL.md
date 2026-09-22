---
name: khenrix-quality
description: >-
  Use when the user names khenrix-quality or the former natural-language modes
  i-have-adhd, no-ai-slop, ponytail, ponytail-review, ponytail-audit,
  ponytail-debt, ponytail-gain, or ponytail-help. Handles ADHD mode, AI-slop
  edit/detect, lite/full/ultra code minimalism, simplification review or audit,
  debt markers, benchmark figures, and help. Do not use for general
  correctness/security reviews or deep humanization.
license: MIT
metadata:
  version: "1.0.0"
---

# Khenrix quality

Choose the requested mode, read its reference, and apply it only to the stated scope.
This skill collects several compatible quality disciplines without making every task run
every discipline.

## Route the request

| Intent | Mode | Read |
|---|---|---|
| Make responses easier to start and follow; enable or disable ADHD mode | `adhd` | `references/response-structure.md` |
| Minimally edit a supplied draft while preserving its voice and facts | `prose-edit` | `references/prose-quality.md` and `references/prose-checklist.md` |
| Audit a draft for named AI-writing patterns without changing it | `prose-detect` | `references/prose-quality.md` |
| Select lite, full, or ultra minimalism for a coding task | `code` | `references/code-quality.md` |
| Review a diff, audit a repository, inventory debt markers, explain the published benchmark, or show help | `code-workflow` | `references/code-workflows.md` |

If the user asks to humanize text, match a voice sample, deeply rewrite a passage, or
explicitly invokes `khenrix-writing`, use that skill instead. Do not run both prose
rewrite passes over the same artifact.

## Mode contracts

- `adhd`: lead with the answer or smallest action, number bounded steps, keep visible
  state, and leave exactly one next action only while work remains. When activation and
  a task arrive together, put the first task action before the mode confirmation; then
  state that the mode remains active until the user disables it.
- `prose-edit`: make the minimum effective edit, preserve the writer's voice and every
  supported fact, remove every named pattern found in the draft, return the complete
  edited draft, then a short `What changed` section. A repeated dramatic closer is a
  pattern to delete, not voice to preserve.
- `prose-detect`: name each observed pattern, quote its line, and state a short fix. Do
  not rewrite, score the text, or guess who or what wrote it.
- `code`: apply the selected minimalism level after reading the relevant flow. Preserve
  required validation, security, accessibility, observability, error handling, requested
  behavior, repository conventions, and the tests needed to prove the change. When a
  shared behavior fix changes a boundary or its callers, add or update runnable coverage
  that asserts the corrected shared behavior; rerunning existing tests alone is enough
  only when they already prove that exact fix.
- `code-workflow`: review and audit modes are read-only unless the user requests fixes;
  debt mode reports only explicit markers; benchmark mode labels published figures as
  upstream results rather than project-specific measurements.

## Composition and precedence

- One mode owns the target artifact. Combine modes only when their scopes differ. For
  example, ADHD may shape the surrounding response while `prose-edit` controls the draft.
- A task-specific workflow skill controls planning, review, deployment, and delivery.
  The code modes only shape or inspect code inside that workflow.
- Repository conventions, requested formats, supported facts, safety requirements, and
  explicit user instructions outrank every mode here.
- The shared house style already supplies compact response, prose, and full-mode code
  defaults. Do not announce this skill or load all references for ordinary work.

## Former skill names

Treat explicit requests for `i-have-adhd`, `no-ai-slop`, `ponytail`,
`ponytail-review`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, or
`ponytail-help` in natural language as requests for their matching mode above. The
corresponding legacy command forms `/<former-name>`, `$<former-name>`, and
`/skill:<former-name>` are retired; use the `khenrix-quality` invocation and matching
mode instead. Do not tell the user to install the old skills.

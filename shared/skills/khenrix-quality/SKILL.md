---
name: khenrix-quality
description: >-
  Use when the user names khenrix-quality or the former natural-language modes
  i-have-adhd, no-ai-slop, ponytail, ponytail-review, ponytail-audit,
  ponytail-debt, ponytail-gain, or ponytail-help. Handles ADHD mode, AI-slop
  edit/detect, lite/full/ultra code minimalism, explicitly requested quality
  reviews or audits, debt markers, benchmark figures, and help. Generic
  correctness/security reviews stay with their specialist workflow; deep
  humanization belongs to khenrix-writing.
license: MIT
metadata:
  version: "1.1.1"
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

- `adhd`: lead with the answer or smallest action, number bounded steps in visible groups
  of at most five, keep visible
  state, and leave one next action only when reader input is needed; otherwise continue
  authorized work. When activation and a task arrive together, put the first task action
  before the mode confirmation; then
  state that the mode remains active until the user disables it.
- `prose-edit`: make the minimum effective edit, preserve the writer's voice and every
  supported fact, remove empty or staged uses of the named patterns, return the complete
  edited draft inline unless the user names a file to update, then a short `What changed`
  section. Work directly from the supplied draft. A repeated dramatic closer is a
  pattern to delete, not voice to preserve.
- `prose-detect`: name each observed pattern, quote its line, and state a short fix. Do
  not rewrite, score the text, or guess who or what wrote it.
- `code`: apply the selected minimalism level after reading the relevant flow and mapping
  affected callers, tests, fixtures, configuration, and exports. Preserve required
  validation, security, accessibility, observability, error handling, requested
  behavior, repository conventions, and the tests needed to prove the change. When a
  shared behavior fix changes a boundary or its callers, add or update runnable coverage
  that asserts the corrected shared behavior; rerunning existing tests alone is enough
  only when they already prove that exact fix.
- `code-workflow`: explicitly requested quality review and audit cover correctness,
  security, expected load, missing tests, performance, and safe simplification. Honor
  narrower requests and remain read-only unless fixes are requested. In each review,
  audit, or simplification-only response, base deletion findings on use and contract
  evidence; implementation/caller counts alone call for inspection or a conditional
  recommendation. Debt reports only explicit comment markers; benchmark figures are
  upstream results, not measurements of this adaptation or the user's repository.

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

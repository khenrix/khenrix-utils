# Focused code workflows

These workflows inherit the safety boundaries in `code-quality.md`. They supplement the
repository's ordinary correctness, security, performance, architecture, and
maintainability checks.

## Review a diff

Inspect the requested diff for safe simplifications. Report one actionable finding per
line as:

`<file>:L<line>: <tag> <what to simplify>. <replacement>.`

Rank findings by the largest safe simplification. Use:

- `delete`: dead code, speculative features, or unused flexibility.
- `stdlib`: custom code supplied by the language's standard library.
- `native`: code or a dependency supplied by the platform.
- `yagni`: an abstraction with one implementation, an unused setting, or a layer with
  one caller.
- `shrink`: equivalent behavior with fewer concepts or clearer code.

Estimate removable lines or dependencies only when the diff supports the estimate. Do
not apply fixes unless asked. If nothing material can be simplified, say the diff is
already lean.

## Audit a repository

Inspect the requested repository or subtree before judging it. Distinguish dead or unused
abstractions from real extension points and established conventions. Use the same tags,
formatting each finding as:

`<tag> <what to simplify>. <replacement>. [path:line]`

Finish with an evidence-based estimate of removable lines and dependencies when possible.
This is read-only. Report that the repository is already lean when no material safe
reduction exists.

## Inventory debt markers

Search tracked source for explicit `TODO`, `FIXME`, `HACK`, and legacy `ponytail:`
comments. Exclude generated output, dependencies, and version-control data. Do not infer
debt from ordinary implementation choices.

For each marker report:

`<file>:<line> - <debt>. trigger: <when to revisit>.`

Use the comment's stated trigger. Mark `no-trigger` when none is measurable. Group by
file, then give total markers and the count without triggers. Use blame only when asked.
Write a ledger file only when explicitly requested.

## Explain the published benchmark

Present these figures as published benchmark means, never as measurements of the current
repository:

```text
Ponytail agentic benchmark

12 feature tasks       54% less source code
                       22% fewer tokens
                       20% lower cost
                       27% faster

Safety tier            100% safe in the published run
```

The feature-task figures are the corrected agentic results. The older 80-94% single-shot
range is a per-task ceiling from an earlier criticized method. The published benchmark
used real coding-agent sessions against seeded repositories. Never claim project-specific
savings without a measured baseline. For repository-specific evidence, use debt mode to
count explicit markers or audit mode to find supported simplifications.

## Show help

Explain the available `adhd`, `prose-edit`, `prose-detect`, `code`, `review`, `audit`,
`debt`, and `benchmark` modes. Mention that full code mode is the shared default and that
explicit scope and repository conventions take precedence. Refer to `khenrix-writing`
for deeper humanization or voice matching.

# Focused code workflows

Use these workflows when the user requests a khenrix-quality review, audit, debt inventory,
benchmark, or help mode. They inherit the boundaries in `code-quality.md`; task-specific
workflows still own planning and delivery. A generic review request belongs to its
specialist workflow. Honor a narrower request, such as simplification only.

## Evidence for simplification

Apply this rule independently to each review, audit, and simplification-only response.
One implementation or caller invites inspection; counts alone do not support a settled
deletion finding. Before recommending deletion, check the whole tree for uses in tests,
fixtures, configuration strings, exports, and dynamic references, and check public
contracts, supported extension points, and repository conventions. A missing static caller
alone does not prove code is unused. With incomplete evidence, defer the finding or make
removal conditional on establishing no required use or contract. Confidently remove code
when the evidence establishes no required use or contract; do not invent hypothetical
callers to preserve proven unused code.

## Review a change

Inspect the named diff, files, branch, or pull request. With no target, inspect uncommitted
changes, or the last commit when the working tree is clean. Read the connected code:
callers, called functions, tests, and relevant configuration. Trace inputs, stored data,
and outputs. A changed signature or behavior requires checking every caller.

Find the expected load in the README or deployment configuration. Judge a local script
and a service shared by many processes against their actual needs; state material load
assumptions rather than inventing scale requirements.

Check in this order:

1. **Correctness:** wrong results, crashes, edge cases, broken callers, and fixes applied
   in one caller while the shared behavior remains wrong.
2. **Security and data loss:** trust-boundary validation, injection, secrets, authorization,
   unsafe write ordering, and errors that lose or expose data.
3. **Expected load:** races, per-process state that must be shared, unbounded growth,
   repeated work, and queries or algorithms that fail at the supported input size.
4. **Missing tests:** risky behavior or a bug fix without a runnable check that would fail
   when the behavior breaks. Keep useful existing coverage; test counts are not a cap.
5. **Performance:** demonstrated slow paths relative to expected use. Distinguish a real
   regression from an optional small improvement.
6. **Safe simplification:** unnecessary code, duplicate behavior, and avoidable concepts.

Useful simplification tags are `delete`, `reuse`, `stdlib`, `native`, `yagni`, `shrink`,
`merge`, and `split`. Name the existing helper's path for reuse. Merge near-copies that
must change together; split unrelated responsibilities, never by line count. A documented
shortcut with a real limit is an accepted decision unless the required behavior or
expected load crosses that limit.

Every finding needs a concrete input or situation and a supported consequence. Re-read
the relevant lines before reporting it. Do not report speculative worries or style taste.
Propose the smallest complete fix, preserving validation, error handling, and useful tests.

### Report

Lead with the change's purpose, then number findings across the report in impact order.
Use `Must fix` for bugs, security, data loss, or failure at expected load; `Should fix` for
missing tests, material slowness, duplication, or unnecessary complexity; and `Nice to have`
for supported small improvements. Omit empty groups.

Each finding gives a path and line (keep inline snapshot locations as logical references,
not invented local file links), explains the relevant behavior and concrete failure,
recommends a fix, and states the consequence if left unresolved. Use enough plain language
to make the finding actionable; do not force it onto one line. Follow a requested report
format when one is supplied.

Finish with what needs fixing first, or say no material findings were found and describe
what was checked. Estimate removable lines or dependencies only when the evidence supports
the estimate. State material areas not inspected or checks not run without inventing risks.
This is a report: apply no fixes unless requested.

## Audit a repository

Use the same checks, evidence requirements, and report format for the requested repository
or subtree. With no narrower target, audit the repository. Map README, dependencies,
build/deploy configuration, entry points, and tests before following the main data flows.
Read high-impact paths fully: authorization, user input, money, writes, background work,
and shared process state. In a large repository, prioritize where a failure would cost
the most and state which areas were not inspected. Do not cap investigation or omit
supported findings to meet an arbitrary report length. Keep the audit read-only.

## Inventory debt markers

Search tracked source comments for `TODO`, `FIXME`, `HACK`, `shortcut:`, and legacy
`ponytail:` markers. A named custom marker replaces that set unless the user asks to
include it alongside the defaults. Respect language comment syntax, including block
comments. Exclude ignored or generated output, dependencies, and version-control data.
Marker-like string literals,
prose examples, and ordinary keyboard-shortcut comments are not deferrals.

For each actual marker report:

`<file>:<line> - <debt or accepted limit>. trigger: <when to revisit>.`

Take the limit and measurable revisit trigger from the comment. Mark `no-trigger` when
none is stated; do not invent one or infer debt from implementation choices. Group by
file, then give total markers and the count without triggers. Use blame only when asked.
Write a ledger file only when explicitly requested.

## Explain the published benchmark

Present these as upstream Ponytail 5 results, never measurements of this adaptation or
the current repository:

```text
Ponytail 5 agentic benchmark — 39 tasks × 5 runs per arm

53% fewer source lines       45% fewer output tokens
26% lower cost              41% less time
```

Source: [the pinned benchmark report](https://github.com/DietrichGebert/ponytail/blob/09c434ab73ca43761c5d8b0c5d341926c2fba710/benchmarks/results/2026-10-07-agentic.md).
The three arms were Ponytail 5, Ponytail v4.13, and no skill. It used headless
Claude Code 2.1.288, Opus 5.5 at default effort. These figures are
geometric means of per-task median ratios against the no-skill arm. Bash was disabled;
the benchmark harness ran the delivered code and tests afterward, so the run measures
delivered work rather than autonomous debugging.

Only 18 tasks had executable hidden checks: Ponytail 5 passed 87/90, versus 86/90 without
the skill. All arms passed 30/30 runs in the six-task security subset; that is not proof
of universal safety or an improvement over baseline. Test presence on tasks judged to
need one was 98% versus 68%; it does not mean every test was thorough or passed. The
remaining tasks do not establish equivalent correctness. Do not present older
single-shot or 12-task figures as current results.

Never claim project-specific savings without a measured baseline. Use debt mode to count
explicit markers and audit mode for supported repository findings.

## Show help

Explain `adhd`, `prose-edit`, `prose-detect`, `code` with lite/full/ultra levels, `review`,
`audit`, `debt`, `benchmark`, and help. Full code mode is the shared default. Explicit
scope and repository conventions take precedence; review and audit are read-only until
fixes are requested. Refer to `khenrix-writing` for deeper humanization or voice matching.
Do not advertise standalone Ponytail commands, dashboard features, or host integrations
that this composed skill does not install.

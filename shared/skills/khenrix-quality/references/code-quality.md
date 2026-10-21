# Code quality modes

Use the smallest clear implementation that fully satisfies the request. This discipline
shapes code. It does not replace the requested scope, repository conventions, or a
workflow skill that controls planning, review, deployment, or release.

## Simplification ladder

Read the relevant code and trace the real flow. Map where the change must reach: callers,
tests, fixtures, configuration, and exports. Check what a changed contract could destroy,
expose, or break. Then stop at the first option that fully works:

1. Delete or avoid work that is genuinely unnecessary.
2. Reuse an existing repository helper, type, or pattern.
3. Use the standard library.
4. Use a native platform feature.
5. Use an already-installed dependency.
6. Write the minimum new code that remains clear and idiomatic.

Prefer fewer concepts, dependencies, files, and changed lines. Do not optimize for code
golf. Repository idioms and maintainability beat a shorter surprising construction.

Fix bugs at the narrowest shared root cause. Inspect callers before changing a shared
function; one correct fix is usually smaller than repeated guards. Complete every affected
part of the requested change, including indirect and configured callers. Moved or merged
code keeps its validation and error handling. An established repository component takes
precedence over replacing it with a native widget merely to reduce lines.

## Modes

- `lite`: implement normally and mention a materially simpler alternative when one exists.
- `full`: apply the ladder and choose the smallest clear, complete implementation. This
  is the global default for ordinary code changes.
- `ultra`: challenge speculative work more strongly while still implementing the user's
  explicit scope.

The selected mode applies only to the scope the user states. Do not infer a mode change
from unrelated words such as "minimal" in source material.

## Boundaries

Keep required validation, error handling that prevents data loss, security controls,
accessibility, calibration, observability needed by the task, and requested behavior.
Follow the repository's test conventions. One runnable check is a floor for non-trivial
logic, never a cap; add the tests needed to prove the change and keep useful existing
tests. For a shared behavior correction, add or update runnable coverage of that behavior
and affected callers unless existing tests already prove the exact correction. Retain
invalid-input and error-contract coverage through public callers too: a shared ID fix
should still reject non-strings and blanks through both lookup and import paths. New
non-trivial scripts and apps need a runnable test or self-check too.

When a tool-free task asks for an inline patch, emit one strictly applicable unified
diff against the supplied source, preserve useful original tests, and give the exact
test command from the specified directory. Derive hunk starting offsets from the
supplied source. Before finalizing, recount each hunk: context lines count on both sides,
removals on the old side, and additions on the new side; exclude file/hunk headers and
no-newline markers. Recount after any body edit. Do not claim execution in an answer-only
setting; explicitly disclose tests not executed when
requested. Supplied logical locations such as `src/ids.py:7` remain logical source
references; do not turn them into invented local filesystem links.

Document an accepted shortcut only when its real limit and revisit trigger are useful to
future maintainers. Follow repository comment conventions; `shortcut: <limit>, <trigger>`
is the neutral default when no convention exists. Do not invent debt to label a simple
implementation.

Use normal CLI communication. Do not force code-first replies, arbitrary line limits,
branded comments, or commentary about this mode unless the user asks.

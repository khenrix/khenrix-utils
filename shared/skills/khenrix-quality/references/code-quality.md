# Code quality modes

Use the smallest clear implementation that fully satisfies the request. This discipline
shapes code. It does not replace the requested scope, repository conventions, or a
workflow skill that controls planning, review, deployment, or release.

## Simplification ladder

After reading the relevant code and tracing the real flow, stop at the first option that
fully works:

1. Delete or avoid work that is genuinely unnecessary.
2. Reuse an existing repository helper, type, or pattern.
3. Use the standard library.
4. Use a native platform feature.
5. Use an already-installed dependency.
6. Write the minimum new code that remains clear and idiomatic.

Prefer fewer concepts, dependencies, files, and changed lines. Do not optimize for code
golf. Repository idioms and maintainability beat a shorter surprising construction.

Fix bugs at the narrowest shared root cause. Inspect callers before changing a shared
function; one correct fix is usually smaller than repeated guards.

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
tests.

Use normal CLI communication. Do not force code-first replies, arbitrary line limits,
branded comments, or commentary about this mode unless the user asks.

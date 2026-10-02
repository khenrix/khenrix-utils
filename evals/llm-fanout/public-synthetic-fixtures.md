# Public synthetic deterministic exercises

`quality-cases.json` and `forge-fixtures/` are a public deterministic test
derivative. Their 60 answer questions, 12 material code examples, input values,
controller assertions, probe observations and fixture seeds were authored for
this public derivative. They are not the original private campaign casebook or
its sealed oracles, a campaign preregistration, or paid non-inferiority evidence.
The 30 normal questions have one assertion each and the 30 deep questions have
two each, retaining the 90-assertion grading denominator.

The existing schemas, loaders, materializer and probe API are unchanged. Answer
cases retain normal and deep modes; code cases retain review, deep-review,
ingress and fusion modes. The casebook seal covers the exact canonical cases.
The fixture pin covers public source bytes and modes, declared checks, seeds,
controller oracle bytes and probe bytes. Both seals were computed from this
derivative's actual files. Original private seals provide no authority here.

The `held-out/` directory name is retained for loader compatibility. These
public toy assertions still stay outside each executor's input and process.
The deterministic suites exercise source/oracle tampering, protected roots,
peer isolation, shell traversal, lock ordering, retries, binary and symlink
preservation. Initial code examples deliberately fail their behavioral oracle
while passing their declared syntax check.

Passing these tests demonstrates software behavior on public synthetic inputs.
A future genuine campaign must independently approve and bind its actual
inputs and earn its own runtime, delivery, isolation and judging evidence.

`tests/fixtures/fanout-v1-pre-multirepo/` is a separate public synthetic status
fixture; its README describes its preserved original format bindings and its
limited authority.

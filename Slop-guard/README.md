# qwen-coder-env2

Home of **[slop-guard](slop-guard/)** — a CI/CD static analyzer that detects
*AI hallucinations* and *"AI slop"* in pull requests: phantom APIs and
citations, template stubs, dead imports, noise comments, and duplicated
blocks that conventional linters cannot see.

```
.
├── slop-guard/            # the Python package, tests, docs, benchmarks
│   ├── src/slop_guard/    #   source
│   ├── docs/              #   rules · architecture · configuration · how to extend
│   ├── benchmarks/        #   labeled dataset + precision/recall harness
│   └── tests/
├── action.yml             # composite GitHub Action (PR gate + SARIF upload)
└── .github/workflows/     # CI: lint · types · tests · self-scan · SARIF validation
```

## Try it

```bash
pip install ./slop-guard
slop-guard scan ./your-project --fail-on warning
```

## Use it in CI

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: gsait5270-lang/qwen-coder-env2@main
  with:
    pr-mode: "true"
    fail-on: warning
```

Full documentation: [slop-guard/README.md](slop-guard/README.md) ·
[rule reference](slop-guard/docs/rules.md) ·
[architecture](slop-guard/docs/architecture.md)

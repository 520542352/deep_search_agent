# Live evaluation baselines

This directory is reserved for reviewed, versioned metric-only baselines. Do not
commit raw Agent answers, tool outputs, credentials, or production data here.

Create a candidate locally with:

```bash
uv run python -m evals.live --save-baseline eval-results/live-baseline.json
```

After reviewing the metric-only JSON, it can be copied here intentionally. A
later run can compare against it with `--baseline`.

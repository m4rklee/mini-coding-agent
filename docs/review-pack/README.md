# mca Review Pack

## Project pitch

mca (mini coding agent) is a local coding agent that works inside a repository, uses constrained tools, and persists sessions and run artifacts under `.mca/`.

## Architecture map

- CLI and Web entrypoints create an `Mca` runtime.
- Runtime builds prompts, executes tools, writes task state, trace, and report artifacts.
- Tooling is intentionally bounded and supports optional MCP servers plus local skills.

## Benchmark evidence

Benchmark and metrics helpers live under `mca/evaluator.py` and `mca/metrics.py`.

## Sample run artifact list

- `.mca/runs/<run_id>/task_state.json`
- `.mca/runs/<run_id>/trace.jsonl`
- `.mca/runs/<run_id>/report.json`

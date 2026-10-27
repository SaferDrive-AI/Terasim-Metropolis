# Contributing to TeraSim Metropolis

Thank you for your interest in contributing. This guide explains how to propose changes.

## Ground rules

- Discuss larger changes in an issue first, so the design can be agreed before code is
  written.
- Keep pull requests focused: one logical change per pull request, with tests and
  documentation updates for any behavior change.
- Never commit secrets, credentials, private data, recorded video, or maps and timing plans
  you do not have the right to publish. Tests and examples use synthetic data only.
- Do not commit large binary files; reference external storage instead.

## Development setup

```bash
python3.10 -m venv .venv && . .venv/bin/activate
pip install -e packages/terasim -e packages/terasim-nde-nade -e packages/terasim-city \
    -e packages/terasim-service -e "packages/terasim-agent-skill[mcp,report]" \
    -e packages/terasim-tmc-service -e packages/terasim-plugin-sdk
pip install pytest pytest-cov ruff black
(cd packages/terasim-nde-nade && python setup.py build_ext --inplace)
```

`redis-server` must be on `PATH` for the service tests that use a live Redis.

## Tests and style

```bash
python -m pytest tests -q                 # unit and integration tests
python examples/acceptance/run_acceptance.py --out /tmp/acceptance   # end-to-end check
ruff check packages tests                 # lint
black --check packages tests              # formatting (line length 100)
```

Markers: `slow`, `integration`, `requires_sumo`, `requires_gui`. Deselect with
`-m "not slow"`.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make the change with tests; run the test suite and the linters.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages
   (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
4. Open a pull request against `main` and fill in the template.

## Developer Certificate of Origin

Contributions are accepted under the Apache License, Version 2.0. Every commit must carry a
`Signed-off-by:` line certifying the [Developer Certificate of Origin](https://developercertificate.org/)
(`git commit -s`).

## Plug-ins

Traffic-operation modules can be developed and published as separate packages against the
Plugin SDK (`packages/terasim-plugin-sdk`, documentation under `docs/plugins/`) without
changes to the core. Contributions of new plug-ins to `examples/plugins/` are welcome.

# Contributing

Contributions are welcome. The contribution process is described in `CONTRIBUTING.md` at the
root of the repository; issue and pull-request templates are in `.github/`.

Before opening a pull request:

1. **Tests pass**: `python -m pytest tests -q` (see [Testing](testing.md)); add tests for new
   behavior next to the existing suite of the package you change.
2. **Style**: the root `pyproject.toml` configures `black` and `ruff` (line length 100). The
   vendored NVIDIA counting module `terasim_tmc_service/nv_generate_tmc.py` is excluded from
   both, so differences to the provider's version stay reviewable.
3. **Documentation builds**: `mkdocs build --strict` succeeds, and pages that describe what you
   changed are updated. After changing an HTTP endpoint or an MCP tool, regenerate the
   reference pages with `python docs/_scripts/gen_reference.py` and commit the result.
4. **Data**: examples and tests use synthetic data only, such as the demo corridor in
   `examples/corridor_demo`.

To add an operation to the platform without changing its packages, write a plugin with the
[Plugin SDK](../plugins/index.md).

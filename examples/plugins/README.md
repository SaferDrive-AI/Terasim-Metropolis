# Traffic Operation plug-in examples

Installable third-party operations modules built on the Plugin Interface
(`packages/terasim-plugin-sdk`, documented in `docs/plugins/`).

| Example | What it does |
|---|---|
| [`webster_fixed_time`](webster_fixed_time/) | Derives a Webster fixed-time plan from the turning-movement counts and recommends it when it beats the current plan |

Each example is its own Python distribution: install it next to TeraSim Metropolis
(`pip install -e examples/plugins/<name>`) and it appears in `terasim-plugin list`.

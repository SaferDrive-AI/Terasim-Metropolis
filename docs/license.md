# License

TeraSim Metropolis is released under the **Apache License, Version 2.0**. The license text is
in `LICENSE` at the root of the repository; the packages `terasim`, `terasim-nde-nade` and
`terasim-service` carry a copy in their own directories.

## Notices

`NOTICE` at the root of the repository holds the attribution notices that accompany the
software:

- the simulation engine (`packages/terasim`), the naturalistic and adversarial driving
  environment (`packages/terasim-nde-nade`) and parts of the service package
  (`packages/terasim-service`) come from the open-source TeraSim project, originally developed
  at Mcity, University of Michigan, and licensed under the Apache License, Version 2.0;
- `packages/terasim-tmc-service/terasim_tmc_service/nv_generate_tmc.py` contains the
  turning-movement counting functions of a reference script provided by NVIDIA for the
  integration with NVIDIA Metropolis VSS.
- `packages/terasim-nde-nade/terasim_nde_nade/utils/sumo_lane_locator.py` is ported from
  Eclipse SUMO and licensed under EPL-2.0 OR GPL-2.0-or-later (`LICENSES/EPL-2.0.txt`), not
  under the Apache License, Version 2.0.

## Third-party components

The software uses third-party components, among them SUMO, which are licensed under their own
terms. `THIRD_PARTY_NOTICES.md` at the root of the repository lists them, together with the
components contained in the container image, and their licenses.

The documentation site is built with MkDocs and the Material for MkDocs theme.

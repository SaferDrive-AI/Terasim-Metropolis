"""``python -m terasim_plugin_sdk`` runs the ``terasim-plugin`` command line."""

import sys

from terasim_plugin_sdk.cli import main

if __name__ == "__main__":
    sys.exit(main())

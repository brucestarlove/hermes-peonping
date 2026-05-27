"""Make hermes_peonping importable from the repo root without a pip install."""
import sys
from pathlib import Path

# The repo root IS the hermes_peonping package (flat layout).
# Add it to sys.path so `import hermes_peonping` resolves correctly
# whether tests are run with or without `pip install -e .`.
_pkg_root = Path(__file__).resolve().parents[1]
if str(_pkg_root.parent) not in sys.path:
    sys.path.insert(0, str(_pkg_root.parent))

import types as _types
import importlib.util as _ilu

# Register the flat root dir as the hermes_peonping package so relative
# imports inside adapter.py / config.py / mapper.py resolve correctly.
_pkg_name = "hermes_peonping"
if _pkg_name not in sys.modules:
    spec = _ilu.spec_from_file_location(
        _pkg_name,
        _pkg_root / "__init__.py",
        submodule_search_locations=[str(_pkg_root)],
    )
    pkg = _ilu.module_from_spec(spec)
    sys.modules[_pkg_name] = pkg
    spec.loader.exec_module(pkg)

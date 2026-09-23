from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_TESTS_CONFTEST = Path(__file__).parent / "tests" / "conftest.py"
_SPEC = spec_from_file_location("_typeflux_tests_conftest", _TESTS_CONFTEST)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"could not load pytest isolation fixtures from {_TESTS_CONFTEST}")
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

isolated_test_environment = _MODULE.isolated_test_environment

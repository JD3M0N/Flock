import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name, relative_path, clear_modules=None):
    """Load a module from the repo using its file path and local sibling imports."""
    clear_modules = clear_modules or []
    module_path = ROOT / relative_path

    for stale_module in clear_modules:
        sys.modules.pop(stale_module, None)

    sys.path.insert(0, str(module_path.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)

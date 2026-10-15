from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


module_path = Path(__file__).with_name("1.py")
spec = importlib.util.spec_from_file_location("cadence_application", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load the AMVelo application.")
application_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = application_module
spec.loader.exec_module(application_module)
app = application_module.app

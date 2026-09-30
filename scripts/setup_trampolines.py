import os
import sys
from pathlib import Path

appdata_dir = Path(os.environ["LOCALAPPDATA"]) / "PCControlAgent"

agent_code = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

CANONICAL_DIR = Path(r"C:\\Projects\\PCControlAgent").resolve()
CANONICAL_ENTRY = CANONICAL_DIR / "agent.py"

if CANONICAL_ENTRY.is_file():
    os.chdir(str(CANONICAL_DIR))
    if str(CANONICAL_DIR) not in sys.path:
        sys.path.insert(0, str(CANONICAL_DIR))

    if __name__ == "__main__":
        import runpy
        runpy.run_path(str(CANONICAL_ENTRY), run_name="__main__")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("canonical_agent", str(CANONICAL_ENTRY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for k, v in mod.__dict__.items():
            if not k.startswith("__"):
                globals()[k] = v
else:
    raise FileNotFoundError(f"Canonical PCControlAgent not found at {CANONICAL_ENTRY}")
"""

boot_code = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

CANONICAL_DIR = Path(r"C:\\Projects\\PCControlAgent").resolve()
CANONICAL_ENTRY = CANONICAL_DIR / "boot_agent.py"

if CANONICAL_ENTRY.is_file():
    os.chdir(str(CANONICAL_DIR))
    if str(CANONICAL_DIR) not in sys.path:
        sys.path.insert(0, str(CANONICAL_DIR))

    if __name__ == "__main__":
        import runpy
        runpy.run_path(str(CANONICAL_ENTRY), run_name="__main__")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("canonical_boot_agent", str(CANONICAL_ENTRY))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for k, v in mod.__dict__.items():
            if not k.startswith("__"):
                globals()[k] = v
else:
    raise FileNotFoundError(f"Canonical PCControlAgent not found at {CANONICAL_ENTRY}")
"""

(appdata_dir / "agent.py").write_text(agent_code, encoding="utf-8")
(appdata_dir / "boot_agent.py").write_text(boot_code, encoding="utf-8")
print("Trampolines installed successfully in:", appdata_dir)

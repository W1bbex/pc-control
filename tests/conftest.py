import sys, os
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
VDS_DIR = os.path.join(ROOT_DIR, 'vds_bot')
SCRIPTS_DIR = os.path.join(ROOT_DIR, 'scripts')
for p in (ROOT_DIR, VDS_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

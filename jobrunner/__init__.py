"""jobrunner - Airflow-lite Python job runner with a REST API and web UI."""

import os
import sys
from pathlib import Path

__version__ = "0.1.0"

# Extra packages installed from the Packages UI land in <data_dir>/pythonlibs
# (pip --target). That directory is inside the data volume, so the packages
# survive container rebuilds. Add it to sys.path in every process (web,
# workers, beat) so jobs can `import` them without any restart.
_extra_libs = Path(os.environ.get("JOBRUNNER_DATA_DIR", "/srv/jobrunner/data")) / "pythonlibs"
try:
    _extra_libs.mkdir(parents=True, exist_ok=True)
    if str(_extra_libs) not in sys.path:
        sys.path.append(str(_extra_libs))
except OSError:
    pass

# Shared library modules editable in the GUI live in <jobs_dir>/lib and are
# importable from any job or endpoint:  from my_utils import helper
_shared_libs = Path(os.environ.get("JOBRUNNER_JOBS_DIR", "/srv/jobrunner/jobs")) / "lib"
try:
    _shared_libs.mkdir(parents=True, exist_ok=True)
    if str(_shared_libs) not in sys.path:
        sys.path.append(str(_shared_libs))
except OSError:
    pass

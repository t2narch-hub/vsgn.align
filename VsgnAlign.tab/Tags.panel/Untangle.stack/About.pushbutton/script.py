# -*- coding: utf-8 -*-
"""Show Revit, pyRevit, Python and .NET versions."""
__title__ = "About"
__author__ = "Tu Thanh Nhan"
import sys
from pyrevit import HOST_APP, forms

lines = ["Vsgn-Align", "Author: Tu Thanh Nhan", ""]
try:
    lines.append("Revit: {} ({})".format(HOST_APP.version_name, HOST_APP.build))
except Exception:
    pass
try:
    from pyrevit import versionmgr
    lines.append("pyRevit: {}".format(versionmgr.get_pyrevit_version().get_formatted()))
except Exception:
    pass
lines.append("Python: {}".format(sys.version.split("\n")[0]))
try:
    import System
    lines.append(".NET: {}".format(System.Environment.Version))
except Exception:
    pass
try:
    import vsgnalign_core
    lines.append("vsgnalign_core: OK")
except Exception as ex:
    lines.append("vsgnalign_core: ERROR - {}".format(ex))
forms.alert("\n".join(lines), title="Vsgn-Align - About")

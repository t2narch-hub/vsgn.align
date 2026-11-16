# -*- coding: utf-8 -*-
"""Show Vsgn-Align version and Revit information."""
__title__ = "About"
__author__ = "Tu Thanh Nhan"

from pyrevit import HOST_APP, forms

try:
    from vsgnalign_version import __version__ as VERSION
except Exception:
    VERSION = "unknown"

lines = [
    "Vsgn-Align",
    "Version: {}".format(VERSION),
    "Author: Tu Thanh Nhan",
    "",
]
try:
    lines.append("Revit: {} ({})".format(HOST_APP.version_name, HOST_APP.build))
except Exception:
    pass
try:
    import System
    lines.append(".NET: {}".format(System.Environment.Version))
except Exception:
    pass

forms.alert("\n".join(lines), title="Vsgn-Align - About", warn_icon=False)

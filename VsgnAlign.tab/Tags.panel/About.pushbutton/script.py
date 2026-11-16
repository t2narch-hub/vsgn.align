# -*- coding: utf-8 -*-
"""Show Vsgn-Align version, author and Revit version."""
__title__ = "About"
__author__ = "Tu Thanh Nhan"

import io
import os
import json
from pyrevit import HOST_APP, forms


def _version():
    try:
        import vsgnalign_core
        return vsgnalign_core.__version__
    except Exception:
        pass
    try:  # fallback: <extension>/extension.json
        folder = os.path.dirname(os.path.abspath(__file__))
        for _ in range(6):
            path = os.path.join(folder, "extension.json")
            if os.path.isfile(path):
                with io.open(path, encoding="utf-8") as f:
                    return json.load(f).get("version", "unknown")
            folder = os.path.dirname(folder)
    except Exception:
        pass
    return "unknown"


lines = [
    "Vsgn-Align",
    "Version: {}".format(_version()),
    "Author: Tu Thanh Nhan",
    "",
]
try:
    lines.append("Revit: {} ({})".format(HOST_APP.version_name, HOST_APP.build))
except Exception:
    pass

forms.alert("\n".join(lines), title="Vsgn-Align - About", warn_icon=False)

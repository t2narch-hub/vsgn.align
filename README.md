# Vsgn-Align – pyRevit extension

**Author:** Tu Thanh Nhan

Align, distribute, stack, untangle and arrange model elements, tags, text notes,
spot elevations and viewports in Revit.

* Revit **2017 → 2026**, including Revit 2025/2026 (.NET 8) – requires **pyRevit 5.x** or newer.
* Works with both the IronPython 2.7 (default) and CPython 3 engines.
* No administrator rights needed: pyRevit installs per user and the extension loads from GitHub.
* Every command is a single Undo step.

## Commands (tab **Vsgn-Align**)

| Panel | Column 1 | Column 2 | Column 3 |
|---|---|---|---|
| Align | Align Left | Align Top | Distribute Horizontal |
| | Align Center | Align Middle | Distribute Vertical |
| | Align Right | Align Bottom | Stack Vertical |

| Panel | Command | Description |
|---|---|---|
| Tags | Arrange | Place tags around the view crop region (Left & Right / Top & Bottom / All sides) |
| Tags | Untangle Vertical / Horizontal | Reorder tags or spot elevations so their leaders do not cross |
| Tags | About | Show Revit / pyRevit / Python versions (include it when reporting problems) |

Select elements first and click a command, or click a command, pick elements and press **Finish**.

Notes:
* Tags and text notes are measured **without their leaders**, so the text itself is aligned.
* Tags move through *TagHeadPosition*; attached or free leader ends stay where they are.
* **Spot elevations / coordinates / slopes**: only the text moves (leader end and shoulder);
  the measured point never moves, so the value stays correct. Requires Revit 2021 or newer.
* Pinned and grouped elements are skipped.
* Works in plans, sections, elevations, drafting views, sheets (viewports) and 3D views.

## Install

```bat
pyrevit extend ui VsgnAlign https://github.com/<your-username>/Vsgn-Align.git
```

Then click **pyRevit ▸ Reload** in Revit.

## Update

Click **pyRevit ▸ Update**, or run `pyrevit extensions update VsgnAlign`.

## Uninstall

```bat
pyrevit extensions delete VsgnAlign
```

## Credits

Developed by **Tu Thanh Nhan**.


Feature set inspired by the Align add-in by Simon Moreau (BIM 42), original code by
Deyan Nenov. This pyRevit version is a full rewrite in Python.

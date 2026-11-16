# Vsgn-Align – pyRevit extension

**Author:** Tu Thanh Nhan  
**Version:** 1.103

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
| Tags | Untangle Vertical / Horizontal | Reorder tags or spot elevations so their leaders do not cross, then space the texts 15% of the text height apart |
| Tags | Untangle V + Left / Right | Untangle Vertical, then line the texts up on their left / right edges |
| Tags | Untangle H + Top / Bottom | Untangle Horizontal, then line the texts up on the top / bottom of their letters |
| Tags | About | Show Vsgn-Align version, author and Revit version (include it when reporting problems) |

Select elements first and click a command, or click a command, pick elements and press **Finish**.

Notes:
* Tags and text notes are measured **without their leaders**, so the text itself is aligned.
* Tags move through *TagHeadPosition*; leader elbows move with the text, attached or free leader ends stay.
* Stack Vertical and Untangle never overlap texts; the gap is 15% of the text height
  (type *Text Size* x view scale, or the measured height for tags). Vertically the gap is
  measured between the visible letters: 2 mm text -> 0.3 mm on paper. Sizes are re-measured
  after each move and corrected automatically.
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

## Changelog

* **1.103** – New: Untangle V + Left/Right, Untangle H + Top/Bottom. Gap = 15% of the text height. About shows the version correctly (version stored in vsgnalign_core.py and extension.json).
* **1.102** – Vertical gap measured between the visible letters, not the Revit text box (2 mm text -> 0.2 mm gap on paper).
* **1.101** – Stack Vertical / Untangle: no overlapping texts, gap = 10% of the text height, re-measure and correct after moving.
* **1.100** – First versioned release: align, distribute, stack (0.2 mm), untangle (0.2 mm), arrange; spot elevation support.

Versioning: each release adds 0.001; after 1.199 the next release is 2.000.

## Credits

Developed by **Tu Thanh Nhan**.


Feature set inspired by the Align add-in by Simon Moreau (BIM 42), original code by
Deyan Nenov. This pyRevit version is a full rewrite in Python.

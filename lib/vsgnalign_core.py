# -*- coding: utf-8 -*-
"""
vsgnalign_core.py - Core library of the Vsgn-Align pyRevit extension.
Author: Tu Thanh Nhan

Commands:
  Align Left / Center / Right / Top / Middle / Bottom
  Distribute Horizontal / Vertical, Stack Vertical
  Untangle Vertical / Horizontal, Arrange Tags

Supports model elements, tags (incl. room/area/space tags), text notes,
spot elevations / coordinates / slopes, viewports on sheets.
Compatible with Revit 2017 - 2026 (IronPython 2.7 and CPython 3 engines).
All coordinates are computed in the active view plane (Right/Up directions).
"""

from Autodesk.Revit.DB import (
    XYZ, Transaction, ElementTransformUtils, IndependentTag, TextNote,
    ElementId, View, ElementType, FilteredElementCollector,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

try:
    from Autodesk.Revit.DB import SpatialElementTag
except Exception:
    SpatialElementTag = None
try:
    from Autodesk.Revit.DB import SpotDimension
except Exception:
    SpotDimension = None

from pyrevit import revit, forms

__author__ = "Tu Thanh Nhan"

EPS = 1e-9
MM_TO_FT = 1.0 / 304.8
TITLE = "Vsgn-Align"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _key(element):
    return element.Id.ToString()


def _is_invalid_id(eid):
    if eid is None:
        return True
    try:
        return eid == ElementId.InvalidElementId
    except Exception:
        return eid.ToString() == "-1"


def _is_tag(e):
    if isinstance(e, IndependentTag):
        return True
    return SpatialElementTag is not None and isinstance(e, SpatialElementTag)


def _is_spot(e):
    return SpotDimension is not None and isinstance(e, SpotDimension)


def _kind(e):
    if _is_tag(e):
        return "tag"
    if _is_spot(e):
        return "spot"
    if isinstance(e, TextNote):
        return "text"
    return "model"


def _tag_has_leader(tag):
    try:
        return bool(tag.HasLeader)
    except Exception:
        return False


def _text_has_leader(text):
    try:
        return text.LeaderCount > 0
    except Exception:
        try:
            return text.GetLeaders().Count > 0
        except Exception:
            return False


def _spot_has_leader(spot):
    try:
        return bool(spot.HasLeader)          # Revit 2021+
    except Exception:
        pass
    try:
        p = spot.LookupParameter("Leader")
        if p is not None:
            return p.AsInteger() == 1
    except Exception:
        pass
    return True


class Frame(object):
    """2D frame of the view: x = RightDirection, y = UpDirection."""

    def __init__(self, view):
        self.right = view.RightDirection.Normalize()
        self.up = view.UpDirection.Normalize()

    def to2d(self, p):
        return (p.DotProduct(self.right), p.DotProduct(self.up))

    def vec(self, dx, dy):
        return self.right.Multiply(dx).Add(self.up.Multiply(dy))


def _box2d_from_bbox(bb, frame):
    if bb is None:
        return None
    t = bb.Transform
    xs, ys = [], []
    for x in (bb.Min.X, bb.Max.X):
        for y in (bb.Min.Y, bb.Max.Y):
            for z in (bb.Min.Z, bb.Max.Z):
                p = XYZ(x, y, z)
                if t is not None:
                    p = t.OfPoint(p)
                u, v = frame.to2d(p)
                xs.append(u)
                ys.append(v)
    return (min(xs), min(ys), max(xs), max(ys))


def box2d(element, view, frame):
    bb = None
    try:
        bb = element.get_BoundingBox(view)
    except Exception:
        pass
    if bb is None:
        try:
            bb = element.get_BoundingBox(None)
        except Exception:
            pass
    return _box2d_from_bbox(bb, frame)


class Item(object):
    def __init__(self, element, kind, box):
        self.element = element
        self.kind = kind
        self.min_x, self.min_y, self.max_x, self.max_y = box
        self.dx = 0.0
        self.dy = 0.0
        self.anchor = None

    w = property(lambda s: s.max_x - s.min_x)
    h = property(lambda s: s.max_y - s.min_y)
    cx = property(lambda s: (s.min_x + s.max_x) / 2.0)
    cy = property(lambda s: (s.min_y + s.max_y) / 2.0)


def _quiet(t):
    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetClearAfterRollback(True)
        opts.SetForcedModalHandling(False)
        t.SetFailureHandlingOptions(opts)
    except Exception:
        pass


def _rollback(t):
    try:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------
def _is_candidate(e):
    if e is None or isinstance(e, View) or isinstance(e, ElementType):
        return False
    return e.Category is not None


def _is_leader_annotation(e):
    return _is_tag(e) or _is_spot(e)


class _PickFilter(ISelectionFilter):
    __namespace__ = "VsgnAlign"  # required by pythonnet 3 (CPython engine)

    def __init__(self, annotations_only=False):
        self.annotations_only = annotations_only

    def AllowElement(self, e):
        if self.annotations_only:
            return _is_leader_annotation(e)
        return _is_candidate(e)

    def AllowReference(self, reference, position):
        return False


def _pick(uidoc, prompt, annotations_only=False):
    doc = uidoc.Document
    test = _is_leader_annotation if annotations_only else _is_candidate
    elements = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    elements = [e for e in elements if e is not None and test(e)]
    if elements:
        return elements
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, _PickFilter(annotations_only), prompt)
    except OperationCanceledException:
        return []
    return [doc.GetElement(r) for r in refs if doc.GetElement(r) is not None]


def _filter_movable(elements):
    ok, skipped = [], 0
    for e in elements:
        try:
            if e.Pinned or not _is_invalid_id(e.GroupId):
                skipped += 1
                continue
        except Exception:
            pass
        ok.append(e)
    return ok, skipped


# --------------------------------------------------------------------------
# Spot dimensions: move only the text (leader end + shoulder), never the origin
# --------------------------------------------------------------------------
def _move_spot(spot, v):
    if _spot_has_leader(spot):
        try:
            end = spot.LeaderEndPosition                    # Revit 2021+
            shoulder = None
            try:
                if spot.LeaderHasShoulder:
                    shoulder = spot.LeaderShoulderPosition
            except Exception:
                shoulder = None
            spot.LeaderEndPosition = end.Add(v)
            if shoulder is not None:
                spot.LeaderShoulderPosition = shoulder.Add(v)
                spot.LeaderEndPosition = end.Add(v)
            return True
        except Exception:
            pass
    try:
        spot.TextPosition = spot.TextPosition.Add(v)        # Revit 2021+
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Measuring: boxes of the text part only (leaders ignored)
# --------------------------------------------------------------------------
def _measure_leader_off(doc, view, frame, elements, boxes):
    targets = [e for e in elements
               if (_kind(e) == "tag" and _tag_has_leader(e))
               or (_kind(e) == "text" and _text_has_leader(e))]
    if not targets:
        return
    t = Transaction(doc, "Vsgn-Align - measure")
    _quiet(t)
    try:
        t.Start()
        for e in targets:
            try:
                if _kind(e) == "tag":
                    e.HasLeader = False
                else:
                    e.RemoveLeaders()
            except Exception:
                pass
        doc.Regenerate()
        for e in targets:
            b = box2d(e, view, frame)
            if b is not None:
                boxes[_key(e)] = b
    except Exception:
        pass
    finally:
        _rollback(t)


def _measure_spots(doc, view, frame, spots, boxes):
    """Shift the spot text far away (+D, then -D) inside rolled-back transactions.
    The far side of the bounding box then belongs to the text only."""
    if not spots:
        return
    dist = {}
    for e in spots:
        b = boxes.get(_key(e))
        size = max(b[2] - b[0], b[3] - b[1]) if b else 1.0
        dist[_key(e)] = size * 4.0 + 10.0

    results = {}
    for sign in (1.0, -1.0):
        t = Transaction(doc, "Vsgn-Align - measure")
        _quiet(t)
        try:
            t.Start()
            moved = []
            for e in spots:
                d = dist[_key(e)] * sign
                if _move_spot(e, frame.vec(d, d)):
                    moved.append(e)
            doc.Regenerate()
            for e in moved:
                b = box2d(e, view, frame)
                if b is not None:
                    results.setdefault(_key(e), {})[sign] = b
        except Exception:
            pass
        finally:
            _rollback(t)

    for e in spots:
        r = results.get(_key(e), {})
        if 1.0 in r and -1.0 in r:
            d = dist[_key(e)]
            box = (r[-1.0][0] + d, r[-1.0][1] + d, r[1.0][2] - d, r[1.0][3] - d)
            if box[0] < box[2] and box[1] < box[3]:
                boxes[_key(e)] = box


def measure(doc, view, frame, elements):
    boxes = {}
    for e in elements:
        b = box2d(e, view, frame)
        if b is not None:
            boxes[_key(e)] = b
    _measure_leader_off(doc, view, frame, elements, boxes)
    _measure_spots(doc, view, frame, [e for e in elements if _kind(e) == "spot"], boxes)
    return boxes


def _make_items(doc, view, frame, elements):
    boxes = measure(doc, view, frame, elements)
    return [Item(e, _kind(e), boxes[_key(e)]) for e in elements if _key(e) in boxes]


# --------------------------------------------------------------------------
# Moving
# --------------------------------------------------------------------------
class _SpotNotMovable(Exception):
    pass


def _move(doc, item, frame):
    if abs(item.dx) < EPS and abs(item.dy) < EPS:
        return
    v = frame.vec(item.dx, item.dy)
    e = item.element
    if item.kind == "spot":
        if not _move_spot(e, v):
            raise _SpotNotMovable()
        return
    if item.kind == "tag":
        try:
            e.TagHeadPosition = e.TagHeadPosition.Add(v)
            return
        except Exception:
            pass
    ElementTransformUtils.MoveElement(doc, e.Id, v)


def _apply(doc, items, frame, name, pre_action=None):
    failed, spot_failed = 0, 0
    t = Transaction(doc, name)
    t.Start()
    try:
        if pre_action is not None:
            pre_action()
        for it in items:
            try:
                _move(doc, it, frame)
            except _SpotNotMovable:
                spot_failed += 1
            except Exception:
                failed += 1
        t.Commit()
    except Exception as ex:
        _rollback(t)
        forms.alert("Command failed:\n{}".format(ex), title=TITLE)
        return
    msgs = []
    if failed:
        msgs.append("{} element(s) could not be moved and were skipped.".format(failed))
    if spot_failed:
        msgs.append("{} spot dimension(s) were skipped. Moving spot text requires "
                    "Revit 2021 or newer.".format(spot_failed))
    if msgs:
        forms.alert("\n".join(msgs), title=TITLE, warn_icon=True)


def _notify_skipped(skipped):
    if not skipped:
        return
    msg = "Skipped {} pinned or grouped element(s).".format(skipped)
    try:
        forms.toast(msg, title=TITLE)
    except Exception:
        print(msg)


def _collect(prompt, min_count, annotations_only=False):
    uidoc = revit.uidoc
    doc = revit.doc
    view = doc.ActiveView
    elements, skipped = _filter_movable(_pick(uidoc, prompt, annotations_only))
    if len(elements) < min_count:
        if elements or skipped:
            forms.alert("Select at least {} movable elements.".format(min_count), title=TITLE)
        return None
    frame = Frame(view)
    items = _make_items(doc, view, frame, elements)
    if len(items) < min_count:
        forms.alert("Could not read the size of the selected elements.", title=TITLE)
        return None
    _notify_skipped(skipped)
    return doc, view, frame, items


# --------------------------------------------------------------------------
# Align
# --------------------------------------------------------------------------
def align(mode):
    """mode: left | center | right | top | middle | bottom"""
    res = _collect("Select elements to align, then click Finish", 2)
    if res is None:
        return
    doc, view, frame, items = res

    if mode == "left":
        target = min(i.min_x for i in items)
        for i in items:
            i.dx = target - i.min_x
    elif mode == "right":
        target = max(i.max_x for i in items)
        for i in items:
            i.dx = target - i.max_x
    elif mode == "center":
        target = (min(i.min_x for i in items) + max(i.max_x for i in items)) / 2.0
        for i in items:
            i.dx = target - i.cx
    elif mode == "top":
        target = max(i.max_y for i in items)
        for i in items:
            i.dy = target - i.max_y
    elif mode == "bottom":
        target = min(i.min_y for i in items)
        for i in items:
            i.dy = target - i.min_y
    elif mode == "middle":
        target = (min(i.min_y for i in items) + max(i.max_y for i in items)) / 2.0
        for i in items:
            i.dy = target - i.cy
    else:
        raise ValueError(mode)

    _apply(doc, items, frame, "Vsgn-Align - Align " + mode.capitalize())


# --------------------------------------------------------------------------
# Distribute (equal gaps, outermost elements stay) and Stack
# --------------------------------------------------------------------------
def distribute(direction):
    """direction: horizontal | vertical"""
    res = _collect("Select at least 3 elements to distribute, then click Finish", 3)
    if res is None:
        return
    doc, view, frame, items = res

    if direction == "horizontal":
        seq = sorted(items, key=lambda i: i.cx)
        gap = (seq[-1].max_x - seq[0].min_x - sum(i.w for i in seq)) / float(len(seq) - 1)
        cursor = seq[0].max_x + gap
        for i in seq[1:-1]:
            i.dx = cursor - i.min_x
            cursor += i.w + gap
    else:
        seq = sorted(items, key=lambda i: -i.cy)
        gap = (seq[0].max_y - seq[-1].min_y - sum(i.h for i in seq)) / float(len(seq) - 1)
        cursor = seq[0].min_y - gap
        for i in seq[1:-1]:
            i.dy = cursor - i.max_y
            cursor -= i.h + gap

    _apply(doc, items, frame, "Vsgn-Align - Distribute " + direction.capitalize())


def stack_vertical(gap_mm=1.5):
    """Stack elements top-down with a fixed paper gap; the top element stays."""
    res = _collect("Select elements to stack, then click Finish", 2)
    if res is None:
        return
    doc, view, frame, items = res
    gap = gap_mm * MM_TO_FT * _scale(view)
    seq = sorted(items, key=lambda i: -i.cy)
    cursor = seq[0].min_y - gap
    for i in seq[1:]:
        i.dy = cursor - i.max_y
        cursor -= i.h + gap
    _apply(doc, items, frame, "Vsgn-Align - Stack Vertical")


def _scale(view):
    try:
        return max(int(view.Scale), 1)
    except Exception:
        return 1


# --------------------------------------------------------------------------
# Anchor point of a leader annotation (what the leader points to)
# --------------------------------------------------------------------------
def _tagged_element(doc, tag):
    try:  # Revit 2022+
        for eid in tag.GetTaggedLocalElementIds():
            e = doc.GetElement(eid)
            if e is not None:
                return e
    except Exception:
        pass
    try:  # Revit 2018 - 2024
        e = tag.GetTaggedLocalElement()
        if e is not None:
            return e
    except Exception:
        pass
    try:  # Revit 2017
        e = doc.GetElement(tag.TaggedLocalElementId)
        if e is not None:
            return e
    except Exception:
        pass
    for attr in ("Room", "Space", "Area"):
        try:
            e = getattr(tag, attr)
            if e is not None:
                return e
        except Exception:
            pass
    return None


def _free_leader_end(tag):
    try:
        if str(tag.LeaderEndCondition) != "Free":
            return None
    except Exception:
        return None
    try:  # Revit 2022+
        refs = tag.GetTaggedReferences()
        if refs is not None and refs.Count > 0:
            return tag.GetLeaderEnd(refs[0])
    except Exception:
        pass
    try:  # Revit <= 2021
        return tag.LeaderEnd
    except Exception:
        return None


def anchor_of(doc, e, view, frame):
    if _is_spot(e):
        try:
            return frame.to2d(e.Origin)
        except Exception:
            return None
    if _tag_has_leader(e):
        p = _free_leader_end(e)
        if p is not None:
            return frame.to2d(p)
    t = _tagged_element(doc, e)
    if t is not None:
        b = box2d(t, view, frame)
        if b is not None:
            return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
        try:
            return frame.to2d(t.Location.Point)
        except Exception:
            pass
    try:
        return frame.to2d(e.TagHeadPosition)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Untangle: reorder tags / spots so leaders do not cross
# --------------------------------------------------------------------------
def untangle(direction):
    """direction: vertical (column of labels) | horizontal (row of labels)"""
    res = _collect("Select tags or spot elevations to untangle, then click Finish",
                   2, annotations_only=True)
    if res is None:
        return
    doc, view, frame, items = res
    for i in items:
        i.anchor = anchor_of(doc, i.element, view, frame)
    items = [i for i in items if i.anchor is not None]
    if len(items) < 2:
        forms.alert("Select at least 2 tags or spot elevations.", title=TITLE)
        return

    if direction == "vertical":
        slots = sorted([i.cy for i in items], reverse=True)
        for slot, i in zip(slots, sorted(items, key=lambda i: -i.anchor[1])):
            i.dy = slot - i.cy
    else:
        slots = sorted([i.cx for i in items])
        for slot, i in zip(slots, sorted(items, key=lambda i: i.anchor[0])):
            i.dx = slot - i.cx

    _apply(doc, items, frame, "Vsgn-Align - Untangle " + direction.capitalize())


# --------------------------------------------------------------------------
# Arrange: place tags neatly around the view crop region
# --------------------------------------------------------------------------
def _view_region(view, frame, items):
    try:
        if view.CropBoxActive:
            r = _box2d_from_bbox(view.CropBox, frame)
            if r is not None:
                return r
    except Exception:
        pass
    xs = [i.anchor[0] for i in items]
    ys = [i.anchor[1] for i in items]
    pad = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.05 + 1.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _place_side(items, side, region, margin, gap):
    if not items:
        return
    minx, miny, maxx, maxy = region
    vertical_side = side in ("left", "right")
    if vertical_side:
        seq = sorted(items, key=lambda i: -i.anchor[1])
        desired = [-i.anchor[1] for i in seq]
        sizes = [i.h for i in seq]
    else:
        seq = sorted(items, key=lambda i: i.anchor[0])
        desired = [i.anchor[0] for i in seq]
        sizes = [i.w for i in seq]

    placed = []
    for k in range(len(seq)):
        c = desired[k]
        if k:
            c = max(c, placed[-1] + sizes[k - 1] / 2.0 + sizes[k] / 2.0 + gap)
        placed.append(c)
    shift = (sum(desired) - sum(placed)) / float(len(placed))

    for i, c in zip(seq, placed):
        c += shift
        if vertical_side:
            new_cy = -c
            new_cx = (minx - margin - i.w / 2.0) if side == "left" else (maxx + margin + i.w / 2.0)
        else:
            new_cx = c
            new_cy = (maxy + margin + i.h / 2.0) if side == "top" else (miny - margin - i.h / 2.0)
        i.dx = new_cx - i.cx
        i.dy = new_cy - i.cy


def arrange():
    uidoc = revit.uidoc
    doc = revit.doc
    view = doc.ActiveView

    opt_lr, opt_tb, opt_all = "Left & Right", "Top & Bottom", "All sides (nearest)"
    mode = forms.CommandSwitchWindow.show(
        [opt_lr, opt_tb, opt_all], message="Arrange tags around the view:")
    if not mode:
        return

    tags = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    tags = [e for e in tags if e is not None and _is_tag(e)]
    if not tags:
        tags = list(FilteredElementCollector(doc, view.Id).OfClass(IndependentTag).ToElements())
        if SpatialElementTag is not None:
            try:
                tags += list(FilteredElementCollector(doc, view.Id)
                             .OfClass(SpatialElementTag).ToElements())
            except Exception:
                pass
    tags, skipped = _filter_movable(tags)
    if not tags:
        forms.alert("No movable tags found in the active view.", title=TITLE)
        return

    frame = Frame(view)
    anchors = dict((_key(t), anchor_of(doc, t, view, frame)) for t in tags)
    items = _make_items(doc, view, frame, tags)
    for i in items:
        i.anchor = anchors.get(_key(i.element))
    items = [i for i in items if i.anchor is not None]
    if not items:
        return

    region = _view_region(view, frame, items)
    scale = _scale(view)
    margin = 8.0 * MM_TO_FT * scale
    gap = 1.5 * MM_TO_FT * scale
    minx, miny, maxx, maxy = region
    cxr, cyr = (minx + maxx) / 2.0, (miny + maxy) / 2.0

    sides = {"left": [], "right": [], "top": [], "bottom": []}
    for i in items:
        ax, ay = i.anchor
        if mode == opt_lr:
            side = "left" if ax < cxr else "right"
        elif mode == opt_tb:
            side = "top" if ay > cyr else "bottom"
        else:
            d = {"left": ax - minx, "right": maxx - ax, "top": maxy - ay, "bottom": ay - miny}
            side = min(d, key=d.get)
        sides[side].append(i)
    for side, group in sides.items():
        _place_side(group, side, region, margin, gap)

    def turn_on_leaders():
        for i in items:
            try:
                if not i.element.HasLeader:
                    i.element.HasLeader = True
            except Exception:
                pass

    _notify_skipped(skipped)
    _apply(doc, items, frame, "Vsgn-Align - Arrange Tags", pre_action=turn_on_leaders)

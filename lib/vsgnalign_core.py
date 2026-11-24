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
    XYZ, Transaction, SubTransaction, ElementTransformUtils, IndependentTag, TextNote,
    BuiltInParameter,
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
__version__ = "1.109"   # +0.001 per release; after 1.199 -> 2.000

EPS = 1e-9
MM_TO_FT = 1.0 / 304.8
GAP_RATIO = 0.15      # default gap = 15% of the text height (changeable in Settings)
CONFIG_SECTION = "VsgnAlign"
MIN_SHOULDER_MM = 2.0  # shortest horizontal shoulder kept when a text moves
LAYOUT_TOL = 1e-4     # ft (~0.03 mm) - layout is considered settled below this
LAYOUT_MAX_PASSES = 4 # measure -> move -> re-measure passes
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


def _temp_tx(doc, inside):
    if inside:
        return SubTransaction(doc)
    t = Transaction(doc, "Vsgn-Align - measure")
    _quiet(t)
    return t


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
    return _is_tag(e) or _is_spot(e) or isinstance(e, TextNote)


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
def _close(a, b, tol=1e-6):
    return a is not None and b is not None and a.DistanceTo(b) < tol


def _move_spot(spot, v, shoulder_v=None):
    """Move the spot text by v (leader end follows). The shoulder point moves by
    shoulder_v (default: v, i.e. rigid). The measured point (origin) never moves."""
    old_text = None
    try:
        old_text = spot.TextPosition                         # Revit 2021+
    except Exception:
        pass
    if _move_spot_leader(spot, v, shoulder_v):
        # Make sure the text followed the leader; if not, move it explicitly.
        if old_text is not None:
            target = old_text.Add(v)
            try:
                spot.Document.Regenerate()
                if not _close(spot.TextPosition, target):
                    spot.TextPosition = target
            except Exception:
                pass
        return True
    try:
        spot.TextPosition = spot.TextPosition.Add(v)
        return True
    except Exception:
        return False


def _move_spot_leader(spot, v, shoulder_v=None):
    if not _spot_has_leader(spot):
        return False
    try:
        end = spot.LeaderEndPosition                        # Revit 2021+
        shoulder = None
        try:
            if spot.LeaderHasShoulder:
                shoulder = spot.LeaderShoulderPosition
        except Exception:
            shoulder = None
        spot.LeaderEndPosition = end.Add(v)
        if shoulder is not None:
            spot.LeaderShoulderPosition = shoulder.Add(shoulder_v if shoulder_v is not None else v)
            spot.LeaderEndPosition = end.Add(v)
        return True
    except Exception:
        return False


def _min_shoulder(element):
    try:
        return MIN_SHOULDER_MM * MM_TO_FT * _scale(element.Document.ActiveView)
    except Exception:
        return MIN_SHOULDER_MM * MM_TO_FT


def _elbow_shift(frame, elbow, attach, dx, dy, min_len):
    """Shift for a leader elbow whose horizontal segment runs elbow -> attach point
    (spot shoulder, text note leader) when the text moves by (dx, dy):
    the elbow keeps its horizontal position and only follows dy, so the leader keeps
    its shape. It is pushed sideways only when the segment would get shorter than
    min_len or flip to the other side."""
    ex, ey = frame.to2d(elbow)
    ax, ay = frame.to2d(attach)
    side = 1.0 if ax >= ex else -1.0
    limit = min(min_len, side * (ax - ex))
    new_ax = ax + dx
    ddx = 0.0
    if limit > 0 and side * (new_ax - ex) < limit:
        ddx = (new_ax - side * limit) - ex
    return frame.vec(ddx, dy)


def _move_spot_text(spot, frame, dx, dy):
    """Real move of a spot text: elbow (shoulder start) stays, shoulder stretches."""
    v = frame.vec(dx, dy)
    shoulder_v = None
    try:
        if _spot_has_leader(spot) and spot.LeaderHasShoulder:
            shoulder_v = _elbow_shift(frame, spot.LeaderShoulderPosition,
                                      spot.LeaderEndPosition, dx, dy, _min_shoulder(spot))
    except Exception:
        shoulder_v = None
    return _move_spot(spot, v, shoulder_v)


def _move_tag(tag, v):
    """Move the tag head by v; leader elbows move with it so the leader keeps its
    shape. Attached / free leader ends stay where they are."""
    elbows = []
    try:  # Revit 2022+: one elbow per tagged reference
        for r in tag.GetTaggedReferences():
            try:
                if tag.HasLeaderElbow(r):
                    elbows.append((r, tag.GetLeaderElbow(r)))
            except Exception:
                pass
    except Exception:
        pass
    old_elbow = None
    if not elbows:
        try:  # Revit <= 2021
            if tag.HasElbow:
                old_elbow = tag.LeaderElbow
        except Exception:
            old_elbow = None

    tag.TagHeadPosition = tag.TagHeadPosition.Add(v)

    for r, p in elbows:
        try:
            tag.SetLeaderElbow(r, p.Add(v))
        except Exception:
            pass
    if old_elbow is not None:
        try:
            tag.LeaderElbow = old_elbow.Add(v)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Measuring: boxes of the text part only (leaders ignored)
# --------------------------------------------------------------------------
def _measure_leader_off(doc, view, frame, elements, boxes, inside=False):
    targets = [e for e in elements
               if (_kind(e) == "tag" and _tag_has_leader(e))
               or (_kind(e) == "text" and _text_has_leader(e))]
    if not targets:
        return
    t = _temp_tx(doc, inside)
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


def _spot_leader_2d(spot, frame):
    """(leader end, shoulder or None, origin) in view coordinates, or None."""
    if not _spot_has_leader(spot):
        return None
    try:
        end = frame.to2d(spot.LeaderEndPosition)
    except Exception:
        return None
    shoulder = None
    try:
        if spot.LeaderHasShoulder:
            shoulder = frame.to2d(spot.LeaderShoulderPosition)
    except Exception:
        shoulder = None
    try:
        origin = frame.to2d(spot.Origin)
    except Exception:
        origin = None
    return (end, shoulder, origin)


def _measure_spots(doc, view, frame, spots, boxes, inside=False):
    """Box of the spot text only (no leader / shoulder / symbol).
    The text is shifted far away in rolled-back transactions, always in the
    direction its shoulder points (so the text never flips side):
      shift A = (side*D, +D) -> far edge and top of the text,
      shift B = (side*D, -D) -> bottom of the text.
    The near edge of the text is where the shoulder ends (leader end)."""
    if not spots:
        return
    plan = {}
    for e in spots:
        b = boxes.get(_key(e))
        size = max(b[2] - b[0], b[3] - b[1]) if b else 1.0
        dist = size * 4.0 + 10.0
        info = _spot_leader_2d(e, frame)
        side = 1.0
        if info is not None:
            end, shoulder, origin = info
            ref = shoulder if shoulder is not None else origin
            if ref is not None and end[0] < ref[0]:
                side = -1.0
        plan[_key(e)] = (dist, info, side)

    results = {}
    for shift in ("A", "B"):
        t = _temp_tx(doc, inside)
        try:
            t.Start()
            moved = []
            for e in spots:
                dist, info, side = plan[_key(e)]
                up = dist if shift == "A" else -dist
                if info is not None:
                    vec = frame.vec(side * dist, up)
                else:  # no leader: the text moves alone
                    vec = frame.vec(up, up)
                if _move_spot(e, vec):
                    moved.append(e)
            doc.Regenerate()
            for e in moved:
                b = box2d(e, view, frame)
                if b is not None:
                    results.setdefault(_key(e), {})[shift] = b
        except Exception:
            pass
        finally:
            _rollback(t)

    for e in spots:
        r = results.get(_key(e), {})
        if "A" not in r or "B" not in r:
            continue
        dist, info, side = plan[_key(e)]
        a, b = r["A"], r["B"]
        if info is not None:
            top = a[3] - dist
            bottom = b[1] + dist
            near = info[0][0]
            if side > 0:
                box = (near, bottom, a[2] - dist, top)
            else:
                box = (a[0] + dist, bottom, near, top)
        else:
            box = (b[0] + dist, b[1] + dist, a[2] - dist, a[3] - dist)
        if box[0] < box[2] and box[1] < box[3]:
            boxes[_key(e)] = box


def _text_note_box(note, view, frame, measured):
    """Box of the text content of a text note from its alignment point (Coord),
    Width / Height and alignment - leaders are never included. Width / Height are
    paper units, so they are scaled by the view scale; the result is checked
    against the box Revit reports and rejected when it does not fit."""
    try:
        coord = note.Coord
        width = note.Width
        height = note.Height
        base = note.BaseDirection.Normalize()
        up = note.UpDirection.Normalize()
    except Exception:
        return None
    if width <= EPS or height <= EPS:
        return None
    halign = str(getattr(note, "HorizontalAlignment", "Left"))
    valign = str(getattr(note, "VerticalAlignment", "Top"))
    for factor in (float(_scale(view)), 1.0):
        w, h = width * factor, height * factor
        left = {"Center": -w / 2.0, "Right": -w}.get(halign, 0.0)
        top = {"Middle": h / 2.0, "Bottom": h}.get(valign, 0.0)
        xs, ys = [], []
        for bx in (left, left + w):
            for by in (top - h, top):
                p = coord.Add(base.Multiply(bx)).Add(up.Multiply(by))
                u, v = frame.to2d(p)
                xs.append(u)
                ys.append(v)
        box = (min(xs), min(ys), max(xs), max(ys))
        if measured is None:
            return box
        # Height must fit inside the reported box (which may include leaders);
        # horizontally the boxes only need to overlap (Width is the wrap width).
        tol = 0.05 * (measured[3] - measured[1]) + EPS
        fits_v = box[1] >= measured[1] - tol and box[3] <= measured[3] + tol
        overlaps_h = box[0] < measured[2] and box[2] > measured[0]
        if fits_v and overlaps_h:
            return box
    return None


def measure(doc, view, frame, elements, inside=False):
    boxes = {}
    for e in elements:
        b = box2d(e, view, frame)
        if b is not None:
            boxes[_key(e)] = b
    _measure_leader_off(doc, view, frame, elements, boxes, inside)
    _measure_spots(doc, view, frame, [e for e in elements if _kind(e) == "spot"], boxes, inside)
    for e in elements:
        if _kind(e) == "text":
            b = _text_note_box(e, view, frame, boxes.get(_key(e)))
            if b is not None:
                boxes[_key(e)] = b
    return boxes


def _make_items(doc, view, frame, elements, inside=False):
    boxes = measure(doc, view, frame, elements, inside)
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
        if not _move_spot_text(e, frame, item.dx, item.dy):
            raise _SpotNotMovable()
        return
    if item.kind == "tag":
        try:
            _move_tag(e, v)
            return
        except Exception:
            pass
    if item.kind == "text":
        _move_text(doc, e, frame, item.dx, item.dy)
        return
    ElementTransformUtils.MoveElement(doc, e.Id, v)


def _text_leaders(note):
    try:
        return list(note.GetLeaders())
    except Exception:
        return []


def _move_text(doc, note, frame, dx, dy):
    """Move the text; leader arrow points stay, leader elbows keep their horizontal
    position (the horizontal segment to the text stretches)."""
    v = frame.vec(dx, dy)
    saved = []
    for ld in _text_leaders(note):
        try:
            saved.append((ld.End, ld.Elbow, ld.Anchor))
        except Exception:
            saved.append((None, None, None))
    ElementTransformUtils.MoveElement(doc, note.Id, v)
    if not saved:
        return
    leaders = _text_leaders(note)
    if len(leaders) != len(saved):
        return
    min_len = _min_shoulder(note)
    for ld, (end, elbow, anchor) in zip(leaders, saved):
        try:
            if elbow is not None:
                if anchor is not None:
                    ld.Elbow = elbow.Add(_elbow_shift(frame, elbow, anchor, dx, dy, min_len))
                else:
                    ld.Elbow = elbow.Add(frame.vec(0.0, dy))
        except Exception:
            pass
        try:
            if end is not None:
                ld.End = end
        except Exception:
            pass


def _apply(doc, items, frame, name, pre_action=None, recompute=None):
    failed, spot_failed = 0, 0
    t = Transaction(doc, name)
    t.Start()
    try:
        if pre_action is not None:
            pre_action()
        if recompute is not None:
            items = recompute()
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
    _report_failures(failed, spot_failed)


def _set_text_alignment(items, side):
    """Same as Revit's 'Align Text Left / Right / Center' for every text note.
    Returns True when at least one note was changed."""
    try:
        from Autodesk.Revit.DB import HorizontalTextAlignment
        value = getattr(HorizontalTextAlignment, side.capitalize())
    except Exception:
        return False
    changed = False
    for i in items:
        if i.kind != "text":
            continue
        try:
            if i.element.HorizontalAlignment != value:
                i.element.HorizontalAlignment = value
                changed = True
        except Exception:
            pass
    return changed


def _remeasure(doc, view, frame, items):
    """Measure again inside the open transaction; keep anchor / leader data."""
    old = dict((_key(i.element), i) for i in items)
    new = _make_items(doc, view, frame, [i.element for i in items], inside=True)
    for i in new:
        o = old.get(_key(i.element))
        if o is not None:
            i.anchor = o.anchor
            i.leader = getattr(o, "leader", None)
    return new


def _report_failures(failed, spot_failed):
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
    name = "Vsgn-Align - Align " + mode.capitalize()

    if mode in ("left", "right") and any(i.kind == "text" for i in items):
        # Text notes: first align the lines inside each note (Revit "Align Text
        # Left / Right"), then measure again and line the notes up.
        state = {"items": items}

        def set_alignment():
            if _set_text_alignment(items, mode):
                doc.Regenerate()

        def recompute():
            new = _remeasure(doc, view, frame, state["items"])
            _compute_align(new, mode)
            return new

        _apply(doc, items, frame, name, pre_action=set_alignment, recompute=recompute)
        return

    _compute_align(items, mode)
    _apply(doc, items, frame, name)


def _compute_align(items, mode):
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


def stack_vertical():
    """Stack elements top-down without overlapping; gap = 10% of the text height.
    The top element stays in place, horizontal positions are unchanged."""
    res = _collect("Select elements to stack, then click Finish", 2)
    if res is None:
        return
    doc, view, frame, items = res
    order = sorted(items, key=lambda i: -i.cy)
    _sequence_layout(doc, view, frame, items, order, "vertical",
                     "Vsgn-Align - Stack Vertical")


def _text_size(element, view):
    """Printed text height in model units (type 'Text Size' x view scale),
    or None when the element has no Text Size (e.g. tags)."""
    try:
        etype = element.Document.GetElement(element.GetTypeId())
        if etype is None:
            return None
        p = etype.get_Parameter(BuiltInParameter.TEXT_SIZE)
        if p is None or not p.HasValue:
            p = etype.LookupParameter("Text Size")
        if p is not None and p.HasValue and p.AsDouble() > EPS:
            return p.AsDouble() * _scale(view)
    except Exception:
        pass
    return None


def _glyph_pad(item, ts):
    """Empty space Revit keeps above and below the letters inside the text box.
    The visible letters are ts high, centred in the box. Only applied to single-line
    texts; multi-line texts (box > 2.2 x ts) keep their box."""
    if ts is None or item.kind != "spot":
        return 0.0
    if ts < item.h < 2.2 * ts:
        return (item.h - ts) / 2.0
    return 0.0


def _glyph_top(item, ts):
    return item.max_y - _glyph_pad(item, ts)


def _glyph_bottom(item, ts):
    return item.min_y + _glyph_pad(item, ts)


def _align_target(items, align, sizes):
    """Fixed edge the texts are lined up to (computed once, before moving)."""
    if align == "left":
        return min(i.min_x for i in items)
    if align == "right":
        return max(i.max_x for i in items)
    if align == "top":
        return max(_glyph_top(i, sizes.get(_key(i.element))) for i in items)
    if align == "bottom":
        return min(_glyph_bottom(i, sizes.get(_key(i.element))) for i in items)
    return None


def _place_sequence(seq, direction, start, gap, sizes, align=None, target=None):
    """Vertical: gap measured between the visible letters (box padding removed);
    optional align left / right lines the texts up on one edge.
    Horizontal: gap measured between the text boxes; optional align top / bottom."""
    cursor = start
    if direction == "vertical":
        for i in seq:
            ts = sizes.get(_key(i.element))
            pad = _glyph_pad(i, ts)
            i.dy = cursor - (i.max_y - pad)
            if align == "left":
                i.dx = target - i.min_x
            elif align == "right":
                i.dx = target - i.max_x
            else:
                i.dx = 0.0
            cursor -= (i.h - 2.0 * pad) + gap
    else:
        for i in seq:
            ts = sizes.get(_key(i.element))
            i.dx = cursor - i.min_x
            if align == "top":
                i.dy = target - _glyph_top(i, ts)
            elif align == "bottom":
                i.dy = target - _glyph_bottom(i, ts)
            else:
                i.dy = 0.0
            cursor += i.w + gap


def _sequence_layout(doc, view, frame, items, order, direction, name, align=None):
    """Place items one after another (top-down or left-right) in the given order
    with gap = 15% of the largest text height (2 mm text -> 0.3 mm between the
    letters on paper). After each pass the real sizes are
    re-measured and the layout is corrected, so texts never overlap even when a
    spot / tag changes its text box after its leader moves.
    With align left / right, text notes also get Revit's Align Text Left / Right."""
    t = Transaction(doc, name)
    t.Start()
    failed, spot_failed = set(), set()
    try:
        if align in ("left", "right") and any(i.kind == "text" for i in items):
            if _set_text_alignment(items, align):
                doc.Regenerate()
                items = _remeasure(doc, view, frame, items)
                for i in items:
                    if getattr(i, "leader", None) is not None:
                        i.leader = leader_geometry(doc, i.element, frame, i.anchor)

        sizes = dict((_key(i.element), _text_size(i.element, view)) for i in items)
        heights = [sizes[_key(i.element)] or i.h for i in items]
        gap = gap_ratio() * max(heights)
        if direction == "vertical":
            start = max(_glyph_top(i, sizes[_key(i.element)]) for i in items)
        else:
            start = min(i.min_x for i in items)
        target = _align_target(items, align, sizes)
        if callable(order):
            order = order(items, direction, start, gap, sizes, align, target)
        order_keys = [_key(i.element) for i in order]
        elements = [i.element for i in items]

        current = items
        for _ in range(LAYOUT_MAX_PASSES):
            by_key = dict((_key(i.element), i) for i in current)
            seq = [by_key[k] for k in order_keys if k in by_key]
            _place_sequence(seq, direction, start, gap, sizes, align, target)
            moving = [i for i in seq
                      if (abs(i.dx) > LAYOUT_TOL or abs(i.dy) > LAYOUT_TOL)
                      and _key(i.element) not in failed
                      and _key(i.element) not in spot_failed]
            if not moving:
                break
            for i in moving:
                try:
                    _move(doc, i, frame)
                except _SpotNotMovable:
                    spot_failed.add(_key(i.element))
                except Exception:
                    failed.add(_key(i.element))
            doc.Regenerate()
            current = _make_items(doc, view, frame, elements, inside=True)
        t.Commit()
    except Exception as ex:
        _rollback(t)
        forms.alert("Command failed:\n{}".format(ex), title=TITLE)
        return
    _report_failures(len(failed), len(spot_failed))


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
    if isinstance(e, TextNote):
        for ld in _text_leaders(e):
            try:
                return frame.to2d(ld.End)
            except Exception:
                pass
        b = box2d(e, view, frame)
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) if b else None
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
def untangle(direction, align=None):
    """direction: vertical (column of labels) | horizontal (row of labels)
    align: None, or 'left' / 'right' for vertical, 'top' / 'bottom' for horizontal."""
    res = _collect("Select tags, spot elevations or text notes to untangle, then click Finish",
                   2, annotations_only=True)
    if res is None:
        return
    doc, view, frame, items = res
    for i in items:
        i.anchor = anchor_of(doc, i.element, view, frame)
    items = [i for i in items if i.anchor is not None]
    if len(items) < 2:
        forms.alert("Select at least 2 tags, spot elevations or text notes.", title=TITLE)
        return

    for i in items:
        i.leader = leader_geometry(doc, i.element, frame, i.anchor)
    name = "Vsgn-Align - Untangle " + direction.capitalize()
    if align:
        name += " + Align " + align.capitalize()
    _sequence_layout(doc, view, frame, items, _best_order, direction, name, align)


# --------------------------------------------------------------------------
# Leader geometry & crossing-free ordering
# --------------------------------------------------------------------------
def _pt2d(frame, p):
    return frame.to2d(p) if p is not None else None


def leader_geometry(doc, e, frame, anchor):
    """Leader polyline split into (fixed start point, [points that move with the text]).
    Spot: origin -> shoulder -> leader end.  Tag: target -> elbow -> head."""
    moving = []
    if _is_spot(e):
        if not _spot_has_leader(e):
            return None
        try:
            fixed = frame.to2d(e.Origin)
        except Exception:
            fixed = anchor
        try:
            if e.LeaderHasShoulder:
                moving.append((_pt2d(frame, e.LeaderShoulderPosition), "y"))
        except Exception:
            pass
        try:
            moving.append((_pt2d(frame, e.LeaderEndPosition), "xy"))
        except Exception:
            pass
    elif isinstance(e, TextNote):
        leaders = _text_leaders(e)
        if not leaders:
            return None
        ld = leaders[0]
        try:
            fixed = frame.to2d(ld.End)
        except Exception:
            return None
        try:
            moving.append((_pt2d(frame, ld.Elbow), "y"))
        except Exception:
            pass
        try:
            moving.append((_pt2d(frame, ld.Anchor), "xy"))
        except Exception:
            pass
    elif _is_tag(e):
        if not _tag_has_leader(e):
            return None
        fixed = anchor
        try:  # Revit 2022+
            for r in e.GetTaggedReferences():
                try:
                    if e.HasLeaderElbow(r):
                        moving.append((_pt2d(frame, e.GetLeaderElbow(r)), "xy"))
                        break
                except Exception:
                    pass
        except Exception:
            pass
        if not moving:
            try:  # Revit <= 2021
                if e.HasElbow:
                    moving.append((_pt2d(frame, e.LeaderElbow), "xy"))
            except Exception:
                pass
        try:
            moving.append((_pt2d(frame, e.TagHeadPosition), "xy"))
        except Exception:
            pass
    else:
        return None
    moving = [m for m in moving if m[0] is not None]
    if fixed is None or not moving:
        return None
    return (fixed, moving)


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _seg_cross(p1, p2, q1, q2):
    d1 = _orient(q1, q2, p1)
    d2 = _orient(q1, q2, p2)
    d3 = _orient(p1, p2, q1)
    d4 = _orient(p1, p2, q2)
    return d1 * d2 < 0 and d3 * d4 < 0


def _seg_hits_box(p, q, box):
    x0, y0, x1, y1 = box
    for (x, y) in (p, q):
        if x0 < x < x1 and y0 < y < y1:
            return True
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for k in range(4):
        if _seg_cross(p, q, corners[k], corners[(k + 1) % 4]):
            return True
    return False


def _crossings(seq):
    """Number of leader/leader and leader/text crossings for the planned dx, dy."""
    polys, boxes = [], []
    for i in seq:
        if getattr(i, "leader", None):
            fixed, moving = i.leader
            pts = [fixed]
            for (x, y), mode in moving:
                pts.append((x + (i.dx if mode == "xy" else 0.0), y + i.dy))
            polys.append(pts)
        else:
            polys.append(None)
        boxes.append((i.min_x + i.dx, i.min_y + i.dy, i.max_x + i.dx, i.max_y + i.dy))
    # Clearance: a leader passing through OR close beside the end of a text counts
    # as a hit (boxes widened by half a text line). Vertically the boxes are shrunk
    # a little so leaders running through the gap between two rows do not count.
    line_h = min(i.h for i in seq) if seq else 0.0
    cx, cy = 0.5 * line_h, 0.1 * line_h
    boxes = [(b[0] - cx, b[1] + cy, b[2] + cx, b[3] - cy) for b in boxes]
    count = 0
    n = len(seq)
    for a in range(n):
        pa = polys[a]
        if pa is None:
            continue
        segs_a = list(zip(pa[:-1], pa[1:]))
        for b in range(n):
            if b == a:
                continue
            for (p, q) in segs_a:
                if _seg_hits_box(p, q, boxes[b]):
                    count += 1
            pb = polys[b]
            if b > a and pb is not None:
                for (p, q) in segs_a:
                    for (r, s2) in zip(pb[:-1], pb[1:]):
                        if _seg_cross(p, q, r, s2):
                            count += 1
    return count


def _best_order(items, direction, start, gap, sizes, align, target):
    """Pick the order with the fewest crossings. Exhaustive for up to 7 labels,
    otherwise best simple order improved by neighbour swaps. Ties keep the order
    closest to the leader targets (top -> bottom / left -> right)."""
    if direction == "vertical":
        base = sorted(items, key=lambda i: -i.anchor[1])
    else:
        base = sorted(items, key=lambda i: i.anchor[0])
    rank = dict((id(i), k) for k, i in enumerate(base))

    def cost(seq):
        _place_sequence(seq, direction, start, gap, sizes, align, target)
        inversions = 0
        for a in range(len(seq)):
            for b in range(a + 1, len(seq)):
                if rank[id(seq[a])] > rank[id(seq[b])]:
                    inversions += 1
        return _crossings(seq) * 1000 + inversions

    best, best_cost = list(base), cost(list(base))
    if best_cost < 1000:
        return best

    if len(items) <= 7:
        import itertools
        for perm in itertools.permutations(base):
            c = cost(list(perm))
            if c < best_cost:
                best, best_cost = list(perm), c
                if c < 1000 and c == 0:
                    break
        return best

    candidates = [list(base), list(reversed(base)),
                  sorted(items, key=lambda i: i.anchor[0]),
                  sorted(items, key=lambda i: -i.anchor[0]),
                  sorted(items, key=lambda i: i.anchor[1]),
                  sorted(items, key=lambda i: -i.anchor[1])]
    for cand in candidates:
        c = cost(cand)
        if c < best_cost:
            best, best_cost = cand, c
    improved, rounds = True, 0
    while improved and rounds < 50:
        improved, rounds = False, rounds + 1
        for k in range(len(best) - 1):
            trial = list(best)
            trial[k], trial[k + 1] = trial[k + 1], trial[k]
            c = cost(trial)
            if c < best_cost:
                best, best_cost, improved = trial, c, True
    return best


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


# --------------------------------------------------------------------------
# Settings (stored per user in the pyRevit config file)
# --------------------------------------------------------------------------
def gap_ratio():
    """Gap between texts as a fraction of the text height (default 0.15)."""
    try:
        from pyrevit import script
        cfg = script.get_config(CONFIG_SECTION)
        value = float(cfg.get_option("gap_ratio", GAP_RATIO))
        if 0.0 <= value <= 2.0:
            return value
    except Exception:
        pass
    return GAP_RATIO


def settings():
    current = gap_ratio() * 100.0
    answer = forms.ask_for_string(
        default="{:g}".format(current),
        prompt="Gap between texts for Stack Vertical and Untangle,\n"
               "in % of the text height (default {:g}):".format(GAP_RATIO * 100.0),
        title=TITLE + " - Settings")
    if answer is None:
        return
    try:
        value = float(answer.replace(",", ".").replace("%", "").strip())
    except Exception:
        forms.alert("Please enter a number, e.g. 15.", title=TITLE)
        return
    if not 0.0 <= value <= 200.0:
        forms.alert("Please enter a value between 0 and 200.", title=TITLE)
        return
    try:
        from pyrevit import script
        cfg = script.get_config(CONFIG_SECTION)
        cfg.gap_ratio = value / 100.0
        script.save_config()
    except Exception as ex:
        forms.alert("Could not save the setting:\n{}".format(ex), title=TITLE)
        return
    forms.alert("Gap set to {:g}% of the text height.".format(value),
                title=TITLE, warn_icon=False)

"""Portable high-sample tile channels and complete-frame validation.

Tile channels precede downsampling: depth is IEEE f64, linear color is f32,
and object identity is u32, all little endian. A tile is never a final plate.

facet-tile-1 holds a single-instant render. facet-tile-2 holds a
shutter-interval integration: color and depth are time means, object identity
is the dominant contributor, latest.u32 is the most recently visible surface,
and scalar fields carry the slice count and approximation-loss aggregates.
"""
from array import array
import math
import re
import sys

from .errors import require
from .values import digest, integer, scalar

TILE_FORMAT = "facet-tile-1"
TILE_RENDERER = "facet-window-raster-1"
TILE_FORMAT_V2 = "facet-tile-2"
TILE_RENDERER_V2 = "facet-window-raster-2"
CHANNELS = {"depth.f64": ("d", 8, 1), "color.f32": ("f", 4, 3),
            "object.u32": ("I", 4, 1)}
CHANNELS_V2 = {**CHANNELS, "latest.u32": ("I", 4, 1)}
FORMAT_CHANNELS = {TILE_FORMAT: CHANNELS, TILE_FORMAT_V2: CHANNELS_V2}


def formats_for(plan):
    if "slices" in plan:
        return TILE_FORMAT_V2, TILE_RENDERER_V2
    return TILE_FORMAT, TILE_RENDERER


def grid(width, height, edge=64):
    integer(width, "tile frame width", 1, 1024)
    integer(height, "tile frame height", 1, 1024)
    integer(edge, "tile edge", 1, 1024)
    return [(x, y, min(x + edge, width), min(y + edge, height))
            for y in range(0, height, edge) for x in range(0, width, edge)]


def _plan_identity(plan):
    """Digestible render-definition identity; slice geometry is summarized.

    Slice triangles are deterministic from the frozen seal, renderer revision
    and resolved temporal block, so identity only needs their times and counts.
    """
    if "slices" not in plan:
        return plan
    return {**{key: value for key, value in plan.items() if key != "slices"},
            "slices": [{"time": slice_["time"], "triangles": len(slice_["triangles"])}
                       for slice_ in plan["slices"]]}


def fingerprint(exposure, plan, seal_hash, edge):
    request = exposure["request"]
    require(plan["width"] == request["width"] * request["samples"] and
            plan["height"] == request["height"] * request["samples"],
            "tile_extent", "Render plan does not match the exposure", 409)
    tile_format, tile_renderer = formats_for(plan)
    return digest({"format": tile_format, "tile_renderer": tile_renderer,
                   "seal_id": exposure["seal_id"], "seal_hash": seal_hash,
                   "request_hash": exposure["request_hash"], "request": request,
                   "renderer": request["renderer"], "edge": edge,
                   "plan_hash": digest(_plan_identity(plan))})


def validate_bounds(bounds):
    require(isinstance(bounds, (list, tuple)) and len(bounds) == 4,
            "tile_bounds", "Expected four tile bounds", 409)
    for component in bounds:
        integer(component, "tile bound", 0, 1024)
    x0, y0, x1, y1 = bounds
    require(x0 < x1 and y0 < y1, "tile_bounds", "Tile bounds are empty", 409)
    return (x1 - x0) * (y1 - y0)


def validate_partition(records, width, height, edge, expected_fingerprint):
    expected = grid(width, height, edge)
    require(len(records) == len(expected), "tile_closure", "Tile manifest has missing or extra blocks", 409)
    seen = set()
    for record in records:
        ordinal = record["ordinal"]
        require(type(ordinal) is int and 0 <= ordinal < len(expected) and ordinal not in seen,
                "tile_closure", "Tile ordinal is invalid or duplicated", 409)
        seen.add(ordinal)
        bounds = record["bounds"]
        validate_bounds(bounds)
        require(tuple(bounds) == expected[ordinal], "tile_closure", "Tiles overlap or do not cover the declared frame", 409)
        require(record["fingerprint"] == expected_fingerprint, "tile_fingerprint", "Tile belongs to another render definition", 409)
        require(record["state"] in {"pending", "complete"}, "tile_state", "Invalid persisted tile state", 409)
        require((record["state"] == "complete") == (record["payload"] is not None),
                "tile_state", "Tile completion and payload disagree", 409)
    return expected


def _raw(values, code, itemsize):
    require(isinstance(values, array) and values.typecode == code and values.itemsize == itemsize,
            "tile_format", "Unexpected native render channel representation", 409)
    if sys.byteorder == "little":
        return values.tobytes()
    portable = array(code, values)
    portable.byteswap()
    return portable.tobytes()


def _values(raw, code, itemsize):
    result = array(code)
    require(result.itemsize == itemsize, "tile_format", "Platform channel width is unsupported", 409)
    result.frombytes(raw)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def _samples(depth, colors, ids, pixels, objects=None, latest=None):
    require(len(depth) == pixels and len(ids) == pixels and len(colors) == pixels * 3,
            "tile_extent", "Tile channel extent mismatch", 409)
    if latest is not None:
        require(len(latest) == pixels, "tile_extent", "Tile channel extent mismatch", 409)
    for index, (distance, object_id) in enumerate(zip(depth, ids)):
        recent = latest[index] if latest is not None else object_id
        require((math.isfinite(distance) and distance > 0 and object_id != 0 and recent != 0) or
                (distance == math.inf and object_id == 0 and recent == 0),
                "tile_semantics", "Tile depth and background identity disagree", 409)
        if objects is not None:
            if object_id:
                require(str(object_id) in objects, "tile_semantics", "Tile object is absent from the frozen mapping", 409)
            if latest is not None and recent:
                require(str(recent) in objects, "tile_semantics", "Tile object is absent from the frozen mapping", 409)
    require(all(math.isfinite(value) for value in colors),
            "tile_semantics", "Tile color contains nonfinite samples", 409)


def encode(rendered, bounds, expected_fingerprint, blobs, fmt=TILE_FORMAT):
    pixels = validate_bounds(bounds)
    channels = FORMAT_CHANNELS[fmt]
    depth, colors, ids = rendered["depth"], rendered["colors"], rendered["ids"]
    latest = rendered.get("latest") if fmt == TILE_FORMAT_V2 else None
    _samples(depth, colors, ids, pixels, latest=latest)
    fragments = integer(rendered["fragments"], "tile fragments", 0, 2**63 - 1)
    triangles = integer(rendered["triangles"], "tile triangles", 0, 2**63 - 1)
    raw = {"depth.f64": _raw(depth, "d", 8), "color.f32": _raw(colors, "f", 4),
           "object.u32": _raw(ids, "I", 4)}
    if fmt == TILE_FORMAT_V2:
        raw["latest.u32"] = _raw(latest, "I", 4)
    payload = {"format": fmt, "byte_order": "little", "bounds": list(bounds),
               "fingerprint": expected_fingerprint, "fragments": fragments, "triangles": triangles,
               "channels": {name: blobs.put(data) for name, data in raw.items()}}
    if fmt == TILE_FORMAT_V2:
        payload["slices"] = integer(rendered["slices"], "tile slices", 1, 2**31)
        payload["loss_sum"] = scalar(rendered["loss_sum"], "tile loss sum", 0, 2**53)
        payload["loss_max"] = scalar(rendered["loss_max"], "tile loss max", 0, 2**53)
        payload["loss_reference_slices"] = integer(rendered["loss_reference_slices"],
                                                   "tile loss reference", 1, 2**31)
    return payload


def descriptors(payload, bounds, expected_fingerprint):
    """Validate a manifest without reading bytes, including in DB backups."""
    pixels = validate_bounds(bounds)
    require(isinstance(payload, dict) and payload.get("byte_order") == "little",
            "tile_format", "Unknown persisted tile format", 409)
    fmt = payload.get("format")
    require(fmt in FORMAT_CHANNELS, "tile_format", "Unknown persisted tile format", 409)
    channels = FORMAT_CHANNELS[fmt]
    require(payload.get("bounds") == list(bounds) and payload.get("fingerprint") == expected_fingerprint,
            "tile_fingerprint", "Tile payload does not match its manifest", 409)
    found = payload.get("channels")
    require(isinstance(found, dict) and set(found) == set(channels),
            "tile_format", "Tile channel set is incomplete", 409)
    for name, (code, size, stride) in channels.items():
        descriptor = found[name]
        require(isinstance(descriptor, dict) and isinstance(descriptor.get("sha256"), str) and
                re.fullmatch(r"[a-f0-9]{64}", descriptor["sha256"]) and
                type(descriptor.get("size")) is int and descriptor["size"] == pixels * size * stride,
                "tile_extent", "Tile descriptor does not match its channel extent", 409)
    integer(payload.get("fragments"), "tile fragments", 0, 2**63 - 1)
    integer(payload.get("triangles"), "tile triangles", 0, 2**63 - 1)
    if fmt == TILE_FORMAT_V2:
        integer(payload.get("slices"), "tile slices", 1, 2**31)
        integer(payload.get("loss_reference_slices"), "tile loss reference", 1, 2**31)
        scalar(payload.get("loss_sum"), "tile loss sum", 0, 2**53)
        scalar(payload.get("loss_max"), "tile loss max", 0, 2**53)
    return found


def decode(payload, bounds, expected_fingerprint, blobs, objects=None, fmt=None):
    pixels = validate_bounds(bounds)
    found = descriptors(payload, bounds, expected_fingerprint)
    if fmt is not None:
        require(payload["format"] == fmt, "tile_format",
                "Tile format does not match the render definition", 409)
    decoded = {}
    for name, (code, size, stride) in FORMAT_CHANNELS[payload["format"]].items():
        descriptor = found[name]
        raw = blobs.read(descriptor["sha256"])
        require(len(raw) == descriptor["size"], "tile_extent", "Tile content length mismatch", 409)
        decoded[name] = _values(raw, code, size)
    latest = decoded.get("latest.u32")
    _samples(decoded["depth.f64"], decoded["color.f32"], decoded["object.u32"], pixels,
             objects, latest=latest)
    result = {"depth": decoded["depth.f64"], "colors": decoded["color.f32"],
              "ids": decoded["object.u32"],
              "fragments": integer(payload.get("fragments"), "tile fragments", 0, 2**63 - 1),
              "triangles": integer(payload.get("triangles"), "tile triangles", 0, 2**63 - 1)}
    if payload["format"] == TILE_FORMAT_V2:
        result.update(latest=latest,
                      slices=integer(payload.get("slices"), "tile slices", 1, 2**31),
                      loss_sum=scalar(payload.get("loss_sum"), "tile loss sum", 0, 2**53),
                      loss_max=scalar(payload.get("loss_max"), "tile loss max", 0, 2**53),
                      loss_reference_slices=integer(payload.get("loss_reference_slices"),
                                                    "tile loss reference", 1, 2**31))
    return result


def assemble(plan, records, edge, expected_fingerprint, blobs, checkpoint):
    width, height = plan["width"], plan["height"]
    validate_partition(records, width, height, edge, expected_fingerprint)
    require(all(record["state"] == "complete" for record in records),
            "tile_incomplete", "Cannot assemble an unfinished exposure", 409)
    motion = "slices" in plan
    fmt = TILE_FORMAT_V2 if motion else TILE_FORMAT
    depth = array("d", [math.inf]) * (width * height)
    ids = array("I", [0]) * (width * height)
    colors = array("f", [0.]) * (width * height * 3)
    latest = array("I", [0]) * (width * height) if motion else None
    fragments = 0
    loss_sum, loss_max, loss_reference = 0., 0., None
    expected_triangles = (sum(len(slice_["triangles"]) for slice_ in plan["slices"])
                          if motion else len(plan["triangles"]))
    for record in sorted(records, key=lambda value: value["ordinal"]):
        checkpoint()
        x0, y0, x1, y1 = record["bounds"]
        tile = decode(record["payload"], record["bounds"], expected_fingerprint, blobs,
                      plan["objects"], fmt=fmt)
        require(tile["triangles"] == expected_triangles,
                "tile_fingerprint", "Tile triangle count differs from the render plan", 409)
        if motion:
            require(tile["slices"] == len(plan["slices"]),
                    "tile_fingerprint", "Tile slice count differs from the render plan", 409)
            loss_sum += tile["loss_sum"]
            loss_max = max(loss_max, tile["loss_max"])
            loss_reference = tile["loss_reference_slices"]
        local_width = x1 - x0
        for y in range(y0, y1):
            source = (y - y0) * local_width
            target = y * width + x0
            depth[target:target + local_width] = tile["depth"][source:source + local_width]
            ids[target:target + local_width] = tile["ids"][source:source + local_width]
            colors[target * 3:(target + local_width) * 3] = tile["colors"][source * 3:(source + local_width) * 3]
            if motion:
                latest[target:target + local_width] = tile["latest"][source:source + local_width]
        fragments += tile["fragments"]
    checkpoint()
    result = {"depth": depth, "ids": ids, "colors": colors,
              "fragments": fragments, "triangles": expected_triangles}
    if motion:
        result["latest"] = latest
        result["slices"] = len(plan["slices"])
        result["loss"] = {"sum": loss_sum, "max": loss_max, "reference_slices": loss_reference}
    return result

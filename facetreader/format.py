"""Version-one plate semantics, independent of the producer's Python code."""
import hashlib
import json
import math
import re
import struct
from .archive import CHANNELS, CHANNELS_V2, MAX_SIDE, read_members
from .errors import InvalidBundle, require
from .png import decode_rgb

TRUST = {
    "verified": "Internal byte integrity and metadata/channel consistency only",
    "external_authenticity": "Not established: an attacker can replace content and all bundle-local hashes",
    "review_validity": "Unknown: the ZIP contains no proof records or live quarantine state",
    "render_correctness": "Not established: channels are decoded and cross-checked, not independently re-rendered",
}

# Motion-blurred plates (facet-plate-2) declare these semantics verbatim; the
# reader re-states them independently instead of importing producer code.
LOSS_METRIC = "max_linear_rgb_channel_delta_full_vs_even_slices"
ATTRIBUTION = {
    "color": "shutter_interval_mean_linear",
    "object.u32": "dominant_time_contributor",
    "latest.u32": "most_recently_visible_surface",
    "depth.f32": "covered_slice_mean_nearest_forward_distance",
}


def _object(value, field):
    require(isinstance(value, dict), "metadata_shape", field + " must be an object")
    return value


def _integer(value, low, high, field):
    require(type(value) is int and low <= value <= high, "metadata_value", field + " is out of range")
    return value


def _number(value, field):
    require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
            "metadata_value", field + " must be a finite number")
    return value


def _text(value, field):
    require(isinstance(value, str) and bool(value), "metadata_value", field + " must be text")
    return value


def _checksum(value, field):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "metadata_hash", field + " must be a lowercase SHA-256")
    return value


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Metadata is not finite canonical JSON") from exc


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _json(raw, field):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "metadata_json", "Duplicate JSON object key")
            value[key] = item
        return value
    def constant(_):
        raise InvalidBundle("metadata_json", "Non-finite JSON constant")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        canonical(value)  # Also catches overflowing exponents and bad surrogates.
    except InvalidBundle:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Invalid " + field) from exc
    return _object(value, field)


def _objects(seal, objects):
    closure = seal.get("closure")
    require(isinstance(closure, list) and 1 <= len(closure) <= 80,
            "closure", "Expected a nonempty frozen placement closure")
    by_key = {}
    for entry in closure:
        entry = _object(entry, "closure entry")
        placement = _object(entry.get("placement"), "placement")
        key = _text(placement.get("key"), "placement.key")
        require(key not in by_key, "closure", "Frozen placement keys must be unique")
        require(placement.get("visible") is True, "closure", "Frozen closure must contain visible placements")
        _object(entry.get("mesh"), "frozen mesh")
        _object(entry.get("pigment"), "frozen pigment")
        for field in ("mesh_revision", "pigment_revision"):
            _integer(entry.get(field), 1, 2**63-2, field)
        by_key[key] = {"key": key,
                       "mesh_id": _text(placement.get("mesh_id"), "placement.mesh_id"),
                       "pigment_id": _text(placement.get("pigment_id"), "placement.pigment_id")}
    expected = {str(i): by_key[key] for i, key in enumerate(sorted(by_key), 1)}
    require(_object(objects, "objects") == expected, "object_mapping", "Object mapping disagrees with the frozen closure")
    return expected


def _temporal(payload, request, statistics):
    """Cross-check a motion plate's frozen shutter choices and attribution."""
    shutter = _object(request.get("shutter"), "request.shutter")
    resolved = _object(request.get("temporal"), "request.temporal")
    open_ = _number(shutter.get("open"), "shutter.open")
    close = _number(shutter.get("close"), "shutter.close")
    require(close > open_, "temporal", "A motion plate needs a nonempty shutter interval")
    slices = _integer(shutter.get("samples"), 2, 64, "shutter.samples")
    require(resolved.get("motion") is True, "temporal", "Resolved temporal block must record motion")
    require(_integer(resolved.get("slices"), 2, 64, "temporal.slices") == slices and
            _integer(resolved.get("requested_slices"), 2, 64, "temporal.requested_slices") == slices,
            "temporal", "Resolved slice count disagrees with the shutter")
    block = _object(payload.get("temporal"), "payload.temporal")
    require(_number(block.get("open"), "temporal.open") == open_
            and _number(block.get("close"), "temporal.close") == close,
            "temporal", "Temporal block disagrees with the frozen shutter")
    require(block.get("motion") is True and block.get("integration") == "midpoint_rule",
            "temporal", "Unexpected temporal integration")
    require(_integer(block.get("slices"), 2, 64, "temporal.slices") == slices
            and _integer(block.get("requested_slices"), 2, 64, "temporal.requested_slices") == slices,
            "temporal", "Temporal slice counts disagree")
    loss = _object(block.get("loss"), "temporal.loss")
    require(loss.get("metric") == LOSS_METRIC, "temporal", "Unexpected approximation-loss metric")
    require(_integer(loss.get("reference_slices"), 1, 64, "temporal.loss.reference_slices") == (slices + 1) // 2,
            "temporal", "Loss reference is not the even-slice subset")
    mean = _number(loss.get("mean"), "temporal.loss.mean")
    maximum = _number(loss.get("max"), "temporal.loss.max")
    require(0 <= mean <= maximum, "temporal", "Approximation-loss estimate is inconsistent")
    attribution = _object(block.get("attribution"), "temporal.attribution")
    require(attribution == ATTRIBUTION, "temporal", "Attribution semantics changed")
    require(statistics.get("time_slices") == slices and statistics.get("temporal_loss") == loss,
            "temporal", "Statistics disagree with the temporal block")
    return {"shutter": {"open": open_, "close": close, "samples": slices},
            "slices": slices, "integration": "midpoint_rule", "loss": loss}, attribution


def evaluate(raw):
    members = read_members(raw)
    plate = _json(members["plate.json"], "plate.json")
    seal = _json(members["seal.json"], "seal.json")
    payload = _object(plate.get("payload"), "plate.payload")
    frozen = _object(seal.get("payload"), "seal.payload")
    require(payload.get("format") in {"facet-plate-1", "facet-plate-2"}
            and frozen.get("format") == "facet-seal-1",
            "format", "Unsupported plate or seal format")
    motion = payload["format"] == "facet-plate-2"
    require(plate.get("state") == "ready" and seal.get("kind") == "seal"
            and seal.get("state") == "frozen", "metadata_state", "Unexpected embedded plate or seal state")
    plate_id = _text(plate.get("id"), "plate.id")
    exposure_id = _text(plate.get("exposure_id"), "plate.exposure_id")
    seal_id = _text(seal.get("id"), "seal.id")
    require(payload.get("seal_id") == seal_id, "seal_id", "Plate refers to another seal")
    content_hash = _checksum(frozen.get("content_hash"), "content_hash")
    require(_digest({key: value for key, value in frozen.items() if key != "content_hash"}) == content_hash,
            "seal_hash", "Frozen canonical content checksum mismatch")
    require(_checksum(payload.get("seal_hash"), "seal_hash") == content_hash,
            "seal_hash", "Plate checksum does not name its frozen content")
    lens = _object(frozen.get("lens"), "lens")
    require(_checksum(payload.get("lens_hash"), "lens_hash") == _digest(lens),
            "lens_hash", "Plate lens checksum mismatch")
    width = _integer(payload.get("width"), 16, MAX_SIDE, "width")
    height = _integer(payload.get("height"), 16, MAX_SIDE, "height")
    request = _object(payload.get("request"), "request")
    require(type(request.get("width")) is int and request["width"] == width
            and type(request.get("height")) is int and request["height"] == height,
            "extent", "Request dimensions disagree with the plate")
    samples = _integer(request.get("samples"), 1, 2, "samples")
    _text(request.get("renderer"), "request.renderer")
    objects = _objects(frozen, payload.get("objects"))
    descriptors = _object(payload.get("passes"), "passes")
    expected_channels = CHANNELS_V2 if motion else CHANNELS
    require(set(descriptors) == expected_channels,
            "channels", "Channel set does not match the plate format")
    require(set(members) == {"plate.json", "seal.json"} | {"passes/" + name for name in expected_channels},
            "archive_members", "Archive members do not match the plate format")
    for name in sorted(expected_channels):
        descriptor = _object(descriptors[name], "descriptor")
        require(set(descriptor) == {"sha256", "size"}, "descriptor", "Unexpected descriptor fields")
        data = members["passes/" + name]
        size = _integer(descriptor.get("size"), 0, 64*1024*1024, "descriptor.size")
        require(size == len(data), "channel_size", "Channel size mismatch: " + name)
        require(_checksum(descriptor.get("sha256"), "descriptor.sha256") == hashlib.sha256(data).hexdigest(),
                "channel_hash", "Channel checksum mismatch: " + name)
        if not name.endswith(".png"):
            stride = 3 if name == "color.rgb" else 4
            require(size == width * height * stride, "extent", "Raw channel extent mismatch: " + name)
    require(_checksum(payload.get("visual_hash"), "visual_hash") == _digest(descriptors),
            "visual_hash", "Channel manifest checksum mismatch")
    decoded = {name: decode_rgb(members["passes/" + name], width, height)
               for name in ("color.png", "depth.png", "object.png")}
    require(decoded["color.png"] == members["passes/color.rgb"],
            "color_pixels", "PNG pixels disagree with the raw color channel")
    streams = [struct.iter_unpack("<f", members["passes/depth.f32"]),
               struct.iter_unpack("<I", members["passes/object.u32"])]
    if motion:
        streams.append(struct.iter_unpack("<I", members["passes/latest.u32"]))
    covered = 0
    background = 0
    minimum = math.inf
    maximum = 0.
    picks = []
    sample_offsets = {0, width-1, (height//2)*width+width//2, width*height-1}
    first_foreground = False
    first_background = False
    object_preview = decoded["object.png"]
    for offset, values in enumerate(zip(*streams)):
        depth, index = values[0][0], values[1][0]
        recent = values[2][0] if motion else index
        require((recent == 0) == (index == 0), "latest_semantics",
                "Latest-surface and dominant channels disagree about background")
        if recent:
            require(str(recent) in objects, "object_index", "Pixel refers to an unknown object")
        if index == 0:
            require(depth == math.inf, "depth_background", "Background must have positive infinite depth")
            background += 1
            take = not first_background
            first_background = True
        else:
            require(str(index) in objects, "object_index", "Pixel refers to an unknown object")
            require(math.isfinite(depth) and depth > 0,
                    "depth_value", "Foreground depth must be finite and positive")
            covered += 1
            minimum = min(minimum, depth)
            maximum = max(maximum, depth)
            take = not first_foreground
            first_foreground = True
        expected_color = bytes(((index * factor) % 255) if index else 0 for factor in (73, 151, 199))
        require(object_preview[offset*3:offset*3+3] == expected_color,
                "object_preview", "Object preview disagrees with the identifier channel")
        if take or offset in sample_offsets:
            pick = {"x": offset % width, "y": offset // width, "object_index": index,
                    "object": objects.get(str(index)), "depth": depth if index else None,
                    "seal_id": seal_id}
            if motion:
                pick["latest_index"] = recent
                pick["latest_object"] = objects.get(str(recent))
            picks.append(pick)
    statistics = _object(payload.get("statistics"), "statistics")
    require(statistics.get("byte_order") == "little"
            and statistics.get("background_depth") == "positive_infinity"
            and statistics.get("depth_units") == "camera_forward_distance",
            "channel_semantics", "Unexpected raw-channel interpretation")
    require(type(statistics.get("covered_pixels")) is int and statistics["covered_pixels"] == covered,
            "coverage", "Coverage count disagrees with the identifier channel")
    exposure = attribution = None
    if motion:
        exposure, attribution = _temporal(payload, request, statistics)
    summary = {"width": width, "height": height, "samples": samples,
               "covered_pixels": covered, "background_pixels": background,
               "objects": len(objects), "finite_depth_range": [minimum, maximum] if covered else None,
               "depth_units": "camera_forward_distance", "object_id_scope": "local_to_this_plate"}
    if motion:
        summary["time_slices"] = exposure["slices"]
    report = {
        "valid": True, "format": payload["format"],
        "summary": summary,
        "provenance": {"plate_id": plate_id, "exposure_id": exposure_id, "seal_id": seal_id,
                       "seal_hash": content_hash, "lens_hash": payload["lens_hash"],
                       "visual_hash": payload["visual_hash"], "renderer": request["renderer"],
                       "assembly_id": _text(frozen.get("assembly_id"), "assembly_id"),
                       "assembly_revision": _integer(frozen.get("assembly_revision"), 1, 2**63-2, "assembly_revision"),
                       "objects": objects,
                       "frozen_resources": [
                           {"key": item["placement"]["key"],
                            "mesh_id": item["placement"]["mesh_id"], "mesh_revision": item["mesh_revision"],
                            "pigment_id": item["placement"]["pigment_id"], "pigment_revision": item["pigment_revision"]}
                           for item in sorted(frozen["closure"], key=lambda item: item["placement"]["key"])]},
        "channels": descriptors, "picks": picks, "trust": dict(TRUST),
    }
    if motion:
        # Offline results keep the exposure choices and attribution meaning.
        report["exposure"] = exposure
        report["attribution"] = attribution
    return report, members

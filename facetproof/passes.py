import math
import struct
from .pigments import linear_to_srgb, srgb_to_linear
from .png import encode_rgb
from .temporal import LOSS_METRIC


def serialize_passes(plan, rendered, request):
    width, height, samples = request["width"], request["height"], request["samples"]
    motion = "latest" in rendered
    rgb = bytearray(width * height * 3)
    depth = bytearray(width * height * 4)
    ids = bytearray(width * height * 4)
    latest = bytearray(width * height * 4) if motion else None
    depth_rgb = bytearray(width * height * 3)
    object_rgb = bytearray(width * height * 3)
    coverage = 0
    for y in range(height):
        for x in range(width):
            offsets = [(y*samples+sy)*plan["width"] + x*samples+sx
                       for sy in range(samples) for sx in range(samples)]
            nearest = min(offsets, key=lambda p: (rendered["depth"][p], p))
            obj_id = rendered["ids"][nearest]
            distance = rendered["depth"][nearest]
            output = y * width + x
            struct.pack_into("<f", depth, output*4, distance)
            struct.pack_into("<I", ids, output*4, obj_id)
            if motion:
                struct.pack_into("<I", latest, output*4, rendered["latest"][nearest])
            for channel in range(3):
                value = sum(rendered["colors"][p*3+channel] for p in offsets) / len(offsets)
                rgb[output*3+channel] = round(linear_to_srgb(value) * 255)
                object_rgb[output*3+channel] = ((obj_id * (73, 151, 199)[channel]) % 255) if obj_id else 0
                shade = round(255 / (1 + distance)) if math.isfinite(distance) else 0
                depth_rgb[output*3+channel] = shade
            coverage += bool(obj_id)
    passes = {"color.rgb": bytes(rgb), "depth.f32": bytes(depth), "object.u32": bytes(ids),
              "color.png": encode_rgb(width, height, bytes(rgb)),
              "depth.png": encode_rgb(width, height, bytes(depth_rgb)),
              "object.png": encode_rgb(width, height, bytes(object_rgb))}
    if motion:
        passes["latest.u32"] = bytes(latest)
    statistics = {"covered_pixels": coverage, "triangles": rendered["triangles"],
                  "fragments": rendered["fragments"], "background_depth": "positive_infinity",
                  "depth_units": "camera_forward_distance", "byte_order": "little"}
    if motion:
        statistics["time_slices"] = rendered["slices"]
        statistics["temporal_loss"] = {
            "mean": rendered["loss"]["sum"] / (width * height),
            "max": rendered["loss"]["max"],
            "metric": LOSS_METRIC,
            "reference_slices": rendered["loss"]["reference_slices"]}
    return passes, statistics

from .algebra import point, face_normal, unit, sub, dot, compose_matrix
from .camera import frustum_planes, project, view_point
from .clipping import clip_triangle
from .errors import require
from .shading import illuminate
from .values import integer, digest, scalar, object_value
from .pigments import srgb_to_linear

RENDERER_REVISION = "facet-raster-1"
MAX_SOURCE_FACES = 40000
# Slice-triangles are materialized once per attempt and shared by all tiles.
MAX_SLICE_FACES = 120000
MAX_TIME_SLICES = 64


def validate_request(data):
    width = integer(data.get("width", 256), "width", 16, 512)
    height = integer(data.get("height", 256), "height", 16, 512)
    samples = integer(data.get("samples", 1), "samples", 1, 2)
    request = {"width": width, "height": height, "samples": samples,
               "renderer": RENDERER_REVISION}
    shutter = data.get("shutter")
    if shutter is not None:
        object_value(shutter, "shutter")
        open_ = scalar(shutter.get("open", 0.0), "shutter.open", -1000, 1000)
        close = scalar(shutter.get("close", 1.0), "shutter.close", -1000, 1000)
        count = integer(shutter.get("samples", 4), "shutter.samples", 1, MAX_TIME_SLICES)
        require(close >= open_, "shutter", "Shutter closes before it opens")
        if count > 1:
            require(close > open_, "shutter", "A multi-slice shutter needs a nonempty interval")
        request["shutter"] = {"open": open_, "close": close, "samples": count}
    return request


def motion_active(motion):
    return isinstance(motion, dict) and any(abs(component) > 0
                                            for component in motion["translation"] + motion["euler"])


def resolve_temporal(request, seal):
    """Pin the exposure to the sealed scene's shared time axis.

    Placements declare velocity per unit time on an absolute axis; the shutter
    selects a window on that same axis. Static closures collapse to a single
    slice so their render definition is identical to a shutterless request.
    """
    motion = any(motion_active(item["placement"].get("motion")) for item in seal["closure"])
    shutter = request.get("shutter")
    if shutter is None:
        if not motion:
            return request
        temporal = {"open": 0.0, "close": 0.0, "requested_slices": 1, "slices": 1, "motion": True}
    else:
        temporal = {"open": shutter["open"], "close": shutter["close"],
                    "requested_slices": shutter["samples"],
                    "slices": shutter["samples"] if motion else 1, "motion": motion}
    return {**request, "temporal": temporal}


def slice_times(temporal):
    """Midpoint-rule instants; a single slice samples the interval midpoint."""
    count = temporal["slices"]
    open_, close = temporal["open"], temporal["close"]
    return [open_ + (index + .5) * (close - open_) / count for index in range(count)]


def placement_matrix(placement, tau):
    motion = placement.get("motion")
    if not motion_active(motion):
        return placement["matrix"]
    transform = placement["transform"]
    translation = transform.get("translation", [0, 0, 0])
    euler = transform.get("euler", [0, 0, 0])
    return compose_matrix([translation[axis] + tau * motion["translation"][axis] for axis in range(3)],
                          [euler[axis] + tau * motion["euler"][axis] for axis in range(3)],
                          transform.get("scale", [1, 1, 1]))


def build_plan(seal, request):
    width, height = request["width"] * request["samples"], request["height"] * request["samples"]
    lens = seal["lens"]
    planes = frustum_planes(lens, width, height)
    objects = {}
    total_faces = sum(len(item["mesh"]["faces"]) for item in seal["closure"])
    temporal = request.get("temporal")
    times = slice_times(temporal) if temporal is not None else [0.0]
    require(total_faces <= MAX_SOURCE_FACES, "capacity", "Exposure exceeds 40000 source faces", 422)
    require(total_faces * len(times) <= MAX_SLICE_FACES, "capacity",
            "Exposure exceeds the temporal face budget", 422)
    sorted_items = sorted(seal["closure"], key=lambda item: item["placement"]["key"])
    for object_id, item in enumerate(sorted_items, 1):
        placement = item["placement"]
        objects[str(object_id)] = {"key": placement["key"], "mesh_id": placement["mesh_id"],
                                   "pigment_id": placement["pigment_id"]}

    def build_triangles(tau):
        triangles = []
        for object_id, item in enumerate(sorted_items, 1):
            placement, mesh, pigment = item["placement"], item["mesh"], item["pigment"]
            vertices = [point(placement_matrix(placement, tau), v) for v in mesh["vertices"]]
            for face_number, face in enumerate(mesh["faces"]):
                world = [vertices[i] for i in face]
                normal = face_normal(world)
                center = [sum(v[a] for v in world) / 3 for a in range(3)]
                direction = sub(lens["eye"], center)
                if dot(direction, direction) < 1e-12:
                    continue
                view_direction = unit(direction) if lens["projection"] == "perspective" else [-v for v in lens["forward"]]
                if not pigment["two_sided"] and dot(normal, view_direction) <= 0:
                    continue
                color = illuminate(normal, pigment, seal["illumination"], view_direction)
                camera_vertices = [view_point(lens, v) for v in world]
                for clipped in clip_triangle(camera_vertices, planes):
                    triangles.append({"vertices": [project(lens, v, width, height) for v in clipped],
                                      "color": color, "object_id": object_id, "face": face_number})
        return triangles

    plan = {"width": width, "height": height, "objects": objects,
            "projection": lens["projection"],
            "background": [srgb_to_linear(c) for c in seal["illumination"]["background"]],
            "lens_hash": digest(lens), "source_faces": total_faces}
    if len(times) > 1:
        plan["temporal"] = {"open": temporal["open"], "close": temporal["close"],
                            "slices": len(times), "integration": "midpoint_rule", "times": times}
        plan["slices"] = [{"time": tau, "triangles": build_triangles(tau)} for tau in times]
    else:
        plan["triangles"] = build_triangles(times[0])
    return plan

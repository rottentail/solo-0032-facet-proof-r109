from .algebra import transform_matrix
from .camera import validate_lens
from .illumination import validate_illumination
from .errors import require
from .values import identity, text, key, object_value, boolean, vector


class Assemblies:
    def __init__(self, db):
        self.db = db

    def create(self, data):
        object_value(data)
        payload = {"name": text(data.get("name"), "name"), "placements": [],
                   "lens": validate_lens(object_value(data.get("lens", {}), "lens")),
                   "illumination": validate_illumination(object_value(data.get("illumination", {}), "illumination"))}
        with self.db.unit_of_work() as conn:
            return self.db.insert(conn, identity("assembly"), "assembly", "draft", payload)

    def get(self, assembly_id):
        with self.db.unit_of_work() as conn:
            return self.db.get(conn, assembly_id, "assembly")

    def revise(self, assembly_id, data):
        object_value(data)
        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, assembly_id, "assembly")
            require(obj["state"] == "draft", "sealed", "Sealed assembly cannot change", 409)
            payload = obj["payload"]
            require("revision" in data, "revision", "Expected revision is required")
            if "name" in data:
                payload["name"] = text(data["name"], "name")
            if "placements" in data:
                placements = data["placements"]
                require(isinstance(placements, list) and len(placements) <= 80,
                        "placements", "Expected at most 80 placements")
                clean = []
                keys = set()
                for placement in placements:
                    object_value(placement, "placement")
                    label = key(placement.get("key"), "placement.key")
                    require(label not in keys, "duplicate", "Placement keys must be unique")
                    keys.add(label)
                    mesh = self.db.get(conn, placement.get("mesh_id"), "mesh")
                    pigment = self.db.get(conn, placement.get("pigment_id"), "pigment")
                    require(mesh["state"] == "usable", "retired", "Cannot place retired geometry", 409)
                    transform = placement.get("transform", {})
                    object_value(transform, "transform")
                    entry = {"key": label, "mesh_id": mesh["id"], "pigment_id": pigment["id"],
                             "matrix": transform_matrix(transform), "transform": transform,
                             "visible": boolean(placement.get("visible", True), "visible")}
                    motion = placement.get("motion")
                    if motion is not None:
                        # Uniform velocity on the shared absolute time axis:
                        # translation units and euler degrees per unit time.
                        object_value(motion, "motion")
                        entry["motion"] = {
                            "translation": vector(motion.get("translation", [0, 0, 0]),
                                                  "motion.translation", low=-1000, high=1000),
                            "euler": vector(motion.get("euler", [0, 0, 0]),
                                            "motion.euler", low=-3600, high=3600)}
                    clean.append(entry)
                payload["placements"] = clean
            if "lens" in data:
                payload["lens"] = validate_lens(object_value(data["lens"], "lens"))
            if "illumination" in data:
                payload["illumination"] = validate_illumination(object_value(data["illumination"], "illumination"))
            return self.db.update(conn, obj, payload, expected=data["revision"])

    def fork(self, assembly_id, name):
        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, assembly_id, "assembly")
            payload = obj["payload"]
            payload["name"] = text(name, "name")
            payload["derived_from"] = assembly_id
            return self.db.insert(conn, identity("assembly"), "assembly", "draft", payload)

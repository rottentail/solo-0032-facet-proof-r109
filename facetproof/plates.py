import json
import math
import struct
from .errors import require
from .png import inspect_png
from .values import identity, canonical, now, integer, choice

PICK_QUESTIONS = {
    "dominant": "Which part contributed the most shutter time to this pixel's color?",
    "latest": "Which surface most recently covered this pixel before the shutter closed?",
}


class Plates:
    def __init__(self, db, blobs, exposures):
        self.db, self.blobs, self.exposures = db, blobs, exposures

    def _get(self, conn, plate_id, available=True):
        require(isinstance(plate_id, str), "reference", "Plate reference must be a string")
        row = conn.execute("SELECT * FROM plates WHERE id=?", (plate_id,)).fetchone()
        require(row is not None, "not_found", "Plate not found", 404)
        obj = {**dict(row), "payload": json.loads(row["payload"])}
        if available:
            require(obj["state"] == "ready", "plate_state", "Plate unavailable", 409)
        return obj

    def get(self, plate_id):
        with self.db.unit_of_work() as conn:
            return self._get(conn, plate_id, False)

    def finish(self, exposure, payload):
        with self.db.unit_of_work() as conn:
            obj = self.exposures._get(conn, exposure["id"])
            require(obj["state"] == "running" and obj["token"] == exposure["token"] and obj["lease_until"] > now(),
                    "lease_lost", "Cannot commit an unowned exposure", 409)
            require(not obj["cancel_requested"], "cancelled", "Exposure cancellation requested", 409)
            self.exposures.resources.require_allocation(conn, obj)
            plate_id = identity("plate")
            conn.execute("INSERT INTO plates VALUES (?,?,?,?,?)",
                         (plate_id, obj["id"], "ready", canonical(payload), now()))
            conn.execute("UPDATE exposures SET state='succeeded',plate_id=?,token=NULL,lease_until=NULL,updated=? WHERE id=?",
                         (plate_id, now(), obj["id"]))
            self.db.event(conn, obj["id"], "succeeded", {"plate_id": plate_id})
            self.exposures.resources.release(conn, obj['id'], 'succeeded')
            return self._get(conn, plate_id)

    def read_pass(self, plate_id, name):
        with self.db.unit_of_work() as conn:
            plate = self._get(conn, plate_id)
            descriptor = plate["payload"]["passes"].get(name)
            require(descriptor is not None, "pass", "Unknown plate pass", 404)
            return self.blobs.read(descriptor["sha256"])

    def verify(self, plate_id):
        with self.db.unit_of_work() as conn:
            plate = self._get(conn, plate_id)
            return self.verify_payload(plate["payload"])

    def verify_payload(self, payload):
        results = {}
        width, height = payload["width"], payload["height"]
        for name, descriptor in payload["passes"].items():
            data = self.blobs.read(descriptor["sha256"])
            require(len(data) == descriptor["size"], "size", "Plate pass size mismatch", 409)
            if name.endswith(".png"):
                info = inspect_png(data)
                require(info["width"] == width and info["height"] == height, "extent", "PNG extent mismatch", 409)
            else:
                stride = 3 if name == "color.rgb" else 4
                require(len(data) == width*height*stride, "extent", "Raw pass extent mismatch", 409)
            results[name] = descriptor["sha256"]
        return {"valid": True, "passes": results}

    def pick(self, plate_id, x, y, mode=None):
        with self.db.unit_of_work() as conn:
            plate = self._get(conn, plate_id)
            payload = plate["payload"]
            x = integer(x, "x", 0, payload["width"]-1)
            y = integer(y, "y", 0, payload["height"]-1)
            offset = (y*payload["width"]+x) * 4
            if payload["format"] != "facet-plate-2":
                require(mode is None, "pick_mode",
                        "Pick mode is only defined for motion-blurred plates", 400)
                index = struct.unpack_from("<I", self.blobs.read(payload["passes"]["object.u32"]["sha256"]), offset)[0]
                distance = struct.unpack_from("<f", self.blobs.read(payload["passes"]["depth.f32"]["sha256"]), offset)[0]
                return {"x": x, "y": y, "object": payload["objects"].get(str(index)),
                        "depth": distance if math.isfinite(distance) else None,
                        "seal_id": payload["seal_id"]}
            # A smeared pixel holds several surfaces over the shutter interval;
            # the mode declares which question this location answers.
            mode = choice("dominant" if mode is None else mode, "mode", set(PICK_QUESTIONS))
            channel = "object.u32" if mode == "dominant" else "latest.u32"
            index = struct.unpack_from("<I", self.blobs.read(payload["passes"][channel]["sha256"]), offset)[0]
            distance = struct.unpack_from("<f", self.blobs.read(payload["passes"]["depth.f32"]["sha256"]), offset)[0]
            return {"x": x, "y": y, "mode": mode, "question": PICK_QUESTIONS[mode],
                    "object": payload["objects"].get(str(index)),
                    "depth": distance if math.isfinite(distance) else None,
                    "depth_meaning": payload["temporal"]["attribution"]["depth.f32"],
                    "seal_id": payload["seal_id"]}

    def quarantine(self, plate_id, reason):
        from .values import text
        reason = text(reason, "reason", 500)
        with self.db.unit_of_work() as conn:
            plate = self._get(conn, plate_id)
            conn.execute("UPDATE plates SET state='quarantined' WHERE id=?", (plate_id,))
            conn.execute("UPDATE decisions SET state='invalidated' WHERE plate_id=? AND state='accepted'", (plate_id,))
            self.db.event(conn, plate_id, "quarantined", {"reason": reason})
            return self._get(conn, plate_id, False)

    def purge(self, plate_id):
        with self.db.unit_of_work() as conn:
            plate = self._get(conn, plate_id, False)
            require(plate["state"] == "quarantined", "state", "Only quarantined plates may purge", 409)
            conn.execute("UPDATE plates SET state='purged' WHERE id=?", (plate_id,))
            self.db.event(conn, plate_id, "purged", {"retained": "content-addressed bytes"})
            return self._get(conn, plate_id, False)

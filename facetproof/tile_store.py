"""Durable frame manifests and token-checked tile publication.

Content writes happen before these short transactions. Losing publication
rights may retain reusable CAS bytes, never a falsely committed tile.
"""
import json

from .errors import Fault, require
from .tile_protocol import fingerprint, grid, validate_partition, decode, descriptors
from .values import canonical, now


class TileStore:
    def __init__(self, db, exposures, blobs):
        self.db, self.exposures, self.blobs = db, exposures, blobs
        with self.db.unit_of_work() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS facet_tile_frames (
                exposure_id TEXT PRIMARY KEY REFERENCES exposures(id),
                width INTEGER NOT NULL, height INTEGER NOT NULL, tile_size INTEGER NOT NULL,
                total_tiles INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                request_hash TEXT NOT NULL, seal_id TEXT NOT NULL, seal_hash TEXT NOT NULL,
                renderer TEXT NOT NULL, created REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS facet_tiles (
                exposure_id TEXT NOT NULL REFERENCES facet_tile_frames(exposure_id),
                ordinal INTEGER NOT NULL, bounds TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','complete')),
                fingerprint TEXT NOT NULL, payload TEXT,
                producer_token TEXT, updated REAL NOT NULL,
                PRIMARY KEY(exposure_id, ordinal))""")

    @staticmethod
    def snapshot_refs(conn):
        """Read committed checkpoint refs from exactly this DB snapshot.

        No TileStore construction, DDL, live database or content access is
        involved. Duplicate hashes retain their individual source records.
        """
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facet_tiles'").fetchone()
        if not exists:
            return []
        result, sizes = [], {}
        rows = conn.execute("SELECT exposure_id,ordinal,bounds,fingerprint,payload FROM facet_tiles WHERE state='complete' ORDER BY exposure_id,ordinal").fetchall()
        for exposure_id, ordinal, raw_bounds, expected, raw_payload in rows:
            try:
                bounds, payload = json.loads(raw_bounds), json.loads(raw_payload)
            except (ValueError, TypeError) as exc:
                raise Fault("tile_format", "Checkpoint metadata is not valid JSON", 409) from exc
            channels = descriptors(payload, bounds, expected)
            for channel, descriptor in channels.items():
                checksum, size = descriptor["sha256"], descriptor["size"]
                require(checksum not in sizes or sizes[checksum] == size,
                        "tile_extent", "Shared checkpoint descriptors disagree about content size", 409)
                sizes[checksum] = size
                result.append({"sha256": checksum, "size": size, "exposure_id": exposure_id,
                               "ordinal": ordinal, "channel": channel, "fingerprint": expected,
                               "bounds": bounds})
        return result

    def _owned(self, conn, exposure):
        current = self.exposures._get(conn, exposure["id"])
        require(current["state"] == "running" and current["token"] == exposure["token"] and
                current["lease_until"] > now(), "lease_lost", "Cannot commit tiles for an unowned exposure", 409)
        require(not current["cancel_requested"], "cancelled", "Exposure cancellation requested", 409)
        self.exposures.resources.require_allocation(conn, current)
        require(current["seal_id"] == exposure["seal_id"] and current["request"] == exposure["request"] and
                current["request_hash"] == exposure["request_hash"],
                "tile_fingerprint", "Claimed render definition no longer matches the exposure", 409)
        return current

    def assert_owned(self, exposure):
        with self.db.unit_of_work() as conn:
            self._owned(conn, exposure)

    def _records(self, conn, exposure_id):
        rows = conn.execute("SELECT * FROM facet_tiles WHERE exposure_id=? ORDER BY ordinal", (exposure_id,)).fetchall()
        return [{**dict(row), "bounds": json.loads(row["bounds"]),
                 "payload": json.loads(row["payload"]) if row["payload"] is not None else None} for row in rows]

    def prepare(self, exposure, plan, edge):
        bounds = grid(plan["width"], plan["height"], edge)
        with self.db.unit_of_work() as conn:
            current = self._owned(conn, exposure)
            seal = self.db.get(conn, current["seal_id"], "seal")
            seal_hash = seal["payload"]["content_hash"]
            expected = fingerprint(current, plan, seal_hash, edge)
            row = conn.execute("SELECT * FROM facet_tile_frames WHERE exposure_id=?", (current["id"],)).fetchone()
            if row is None:
                conn.execute("INSERT INTO facet_tile_frames VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (current["id"], plan["width"], plan["height"], edge, len(bounds), expected,
                              current["request_hash"], current["seal_id"], seal_hash, current["request"]["renderer"], now()))
                conn.executemany("INSERT INTO facet_tiles VALUES (?,?,?,'pending',?,NULL,NULL,?)",
                                 [(current["id"], ordinal, canonical(block), expected, now()) for ordinal, block in enumerate(bounds)])
                self.db.event(conn, current["id"], "tiles_prepared", {"fingerprint": expected, "tiles": len(bounds), "tile_size": edge})
                row = conn.execute("SELECT * FROM facet_tile_frames WHERE exposure_id=?", (current["id"],)).fetchone()
            require(row["fingerprint"] == expected and row["width"] == plan["width"] and row["height"] == plan["height"] and
                    row["tile_size"] == edge and row["total_tiles"] == len(bounds) and
                    row["request_hash"] == current["request_hash"] and row["seal_id"] == current["seal_id"] and
                    row["seal_hash"] == seal_hash and row["renderer"] == current["request"]["renderer"],
                    "tile_fingerprint", "Existing progress belongs to a different render definition", 409)
            records = self._records(conn, current["id"])
            validate_partition(records, plan["width"], plan["height"], edge, expected)
            return dict(row), records

    def commit(self, exposure, ordinal, payload):
        # Verify bytes before taking the database writer lock; the manifest
        # is checked again under the lock and owns the authoritative bounds.
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT bounds,fingerprint FROM facet_tiles WHERE exposure_id=? AND ordinal=?",
                               (exposure["id"], ordinal)).fetchone()
        finally:
            conn.close()
        require(row is not None, "tile_closure", "Tile has no prepared manifest slot", 409)
        bounds, expected = json.loads(row["bounds"]), row["fingerprint"]
        decode(payload, bounds, expected, self.blobs)
        with self.db.unit_of_work() as conn:
            current = self._owned(conn, exposure)
            row = conn.execute("SELECT * FROM facet_tiles WHERE exposure_id=? AND ordinal=?",
                               (exposure["id"], ordinal)).fetchone()
            require(row is not None and row["fingerprint"] == expected and json.loads(row["bounds"]) == bounds,
                    "tile_fingerprint", "Tile manifest changed while bytes were verified", 409)
            serialized = canonical(payload)
            if row["state"] == "complete":
                require(row["payload"] == serialized, "tile_conflict", "A committed tile cannot be replaced by different samples", 409)
                return False
            require(row["state"] == "pending" and row["payload"] is None,
                    "tile_state", "Tile is not a writable pending slot", 409)
            self._owned(conn, exposure)
            changed = conn.execute("""UPDATE facet_tiles SET state='complete',payload=?,producer_token=?,updated=?
                WHERE exposure_id=? AND ordinal=? AND state='pending' AND fingerprint=?""",
                (serialized, exposure["token"], now(), exposure["id"], ordinal, expected)).rowcount
            require(changed == 1, "tile_conflict", "Tile publication lost its pending slot", 409)
            self.db.event(conn, exposure["id"], "tile_committed", {"ordinal": ordinal, "bounds": bounds,
                          "fingerprint": expected, "token": exposure["token"], "attempt": current["attempt"]})
            return True

    def snapshot(self, exposure_id):
        conn = self.db.connect()
        try:
            conn.execute("BEGIN")
            frame = conn.execute("SELECT * FROM facet_tile_frames WHERE exposure_id=?", (exposure_id,)).fetchone()
            require(frame is not None, "tile_closure", "Exposure has no tile manifest", 409)
            records = self._records(conn, exposure_id)
            validate_partition(records, frame["width"], frame["height"], frame["tile_size"], frame["fingerprint"])
            return dict(frame), records
        finally:
            conn.rollback()
            conn.close()

    def progress(self, exposure_id):
        """Read one SQLite snapshot; committed tiles are not plate success."""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN")
            exposure = self.exposures._get(conn, exposure_id)
            frame = conn.execute("SELECT * FROM facet_tile_frames WHERE exposure_id=?", (exposure_id,)).fetchone()
            if frame is None:
                return {"exposure_id": exposure_id, "state": exposure["state"], "planned": False,
                        "committed_tiles": 0, "total_tiles": 0, "committed_pixels": 0,
                        "total_pixels": 0, "committed_fragments": 0,
                        "complete": False, "fingerprint": None}
            records = self._records(conn, exposure_id)
            validate_partition(records, frame["width"], frame["height"], frame["tile_size"], frame["fingerprint"])
            completed = [row for row in records if row["state"] == "complete"]
            pixels = sum((row["bounds"][2] - row["bounds"][0]) * (row["bounds"][3] - row["bounds"][1]) for row in completed)
            fragments = sum(row["payload"]["fragments"] for row in completed)
            return {"exposure_id": exposure_id, "state": exposure["state"], "planned": True,
                    "committed_tiles": len(completed), "total_tiles": frame["total_tiles"],
                    "committed_pixels": pixels, "total_pixels": frame["width"] * frame["height"],
                    "committed_fragments": fragments,
                    "complete": len(completed) == frame["total_tiles"], "fingerprint": frame["fingerprint"]}
        finally:
            conn.rollback()
            conn.close()

"""Resume high-sample raster windows, then assemble for existing passes."""
from .raster import rasterize
from .temporal import SliceAccumulator
from .tile_protocol import assemble, encode, TILE_FORMAT_V2
from .tile_store import TileStore


class TileEngine:
    def __init__(self, db, exposures, blobs, *, tile_size=64):
        self.store = TileStore(db, exposures, blobs)
        self.blobs, self.tile_size = blobs, tile_size

    def render_window(self, plan, checkpoint, bounds, fingerprint):
        """Render one prepared window into a committable tile payload."""
        if "slices" not in plan:
            return encode(rasterize(plan, checkpoint, bounds), bounds, fingerprint, self.blobs)
        pixels = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        accumulator = SliceAccumulator(pixels, len(plan["objects"]))
        for index, slice_ in enumerate(plan["slices"]):
            checkpoint()
            view = {"width": plan["width"], "height": plan["height"],
                    "projection": plan["projection"], "background": plan["background"],
                    "triangles": slice_["triangles"]}
            accumulator.add(rasterize(view, checkpoint, bounds), even=index % 2 == 0)
        return encode(accumulator.finish(), bounds, fingerprint, self.blobs, TILE_FORMAT_V2)

    def render(self, exposure, plan, checkpoint):
        frame, records = self.store.prepare(exposure, plan, self.tile_size)
        for record in records:
            checkpoint()
            if record["state"] == "complete":
                continue
            # Window bounds stay in the original global sample coordinates.
            # Re-triangulation or shifting float vertices would change edge
            # evaluation and could change f64 depth or coplanar ownership.
            payload = self.render_window(plan, checkpoint, tuple(record["bounds"]),
                                         frame["fingerprint"])
            checkpoint()
            self.store.commit(exposure, record["ordinal"], payload)
        self.store.assert_owned(exposure)
        frame, records = self.store.snapshot(exposure["id"])
        result = assemble(plan, records, frame["tile_size"], frame["fingerprint"], self.blobs, checkpoint)
        self.store.assert_owned(exposure)
        return result

    def progress(self, exposure_id):
        return self.store.progress(exposure_id)

"""Shutter-interval temporal integration for motion-blurred exposures.

Every shutter slice is a complete hidden-surface render at its own instant, so
occlusion changes across the interval are real rather than interpolated.
Slices merge per pixel: color integrates over the whole interval (uncovered
slices contribute the background), depth integrates only over covered slices,
and the two identity channels answer different review questions — the dominant
contributor (most shutter time) and the most recently visible surface.
"""
from array import array
import math

LOSS_METRIC = "max_linear_rgb_channel_delta_full_vs_even_slices"

ATTRIBUTION = {
    "color": "shutter_interval_mean_linear",
    "object.u32": "dominant_time_contributor",
    "latest.u32": "most_recently_visible_surface",
    "depth.f32": "covered_slice_mean_nearest_forward_distance",
}


class SliceAccumulator:
    """Accumulate one window's slice renders into shutter-interval channels."""

    def __init__(self, pixels, objects):
        self.pixels = pixels
        self.stride = objects + 1
        self.colors = array("d", [0.]) * (pixels * 3)
        self.even_colors = array("d", [0.]) * (pixels * 3)
        self.depth_sum = array("d", [0.]) * pixels
        self.covered = array("I", [0]) * pixels
        self.counts = array("I", [0]) * (pixels * self.stride)
        self.latest = array("I", [0]) * pixels
        self.fragments = 0
        self.triangles = 0
        self.slices = 0
        self.even_slices = 0

    def add(self, rendered, even):
        depth, ids, colors = rendered["depth"], rendered["ids"], rendered["colors"]
        counts, covered, latest = self.counts, self.covered, self.latest
        stride = self.stride
        full, half = self.colors, self.even_colors
        for pixel in range(self.pixels):
            base = pixel * 3
            full[base] += colors[base]
            full[base + 1] += colors[base + 1]
            full[base + 2] += colors[base + 2]
            if even:
                half[base] += colors[base]
                half[base + 1] += colors[base + 1]
                half[base + 2] += colors[base + 2]
            object_id = ids[pixel]
            if object_id:
                covered[pixel] += 1
                counts[pixel * stride + object_id] += 1
                latest[pixel] = object_id
                self.depth_sum[pixel] += depth[pixel]
        if even:
            self.even_slices += 1
        self.slices += 1
        self.fragments += rendered["fragments"]
        self.triangles += rendered["triangles"]

    def finish(self):
        slices, even_slices = self.slices, self.even_slices
        colors = array("f", [0.]) * (self.pixels * 3)
        depth = array("d", [math.inf]) * self.pixels
        ids = array("I", [0]) * self.pixels
        loss_sum = 0.
        loss_max = 0.
        for pixel in range(self.pixels):
            base = pixel * 3
            worst = 0.
            for channel in range(3):
                mean = self.colors[base + channel] / slices
                colors[base + channel] = mean
                if even_slices:
                    delta = abs(mean - self.even_colors[base + channel] / even_slices)
                    if delta > worst:
                        worst = delta
            loss_sum += worst
            if worst > loss_max:
                loss_max = worst
            if self.covered[pixel]:
                depth[pixel] = self.depth_sum[pixel] / self.covered[pixel]
                best, best_count = 0, 0
                row = pixel * self.stride
                for object_id in range(1, self.stride):
                    count = self.counts[row + object_id]
                    if count > best_count:
                        best, best_count = object_id, count
                ids[pixel] = best
        return {"depth": depth, "ids": ids, "colors": colors, "latest": self.latest,
                "fragments": self.fragments, "triangles": self.triangles, "slices": slices,
                "loss_sum": loss_sum, "loss_max": loss_max,
                "loss_reference_slices": even_slices}

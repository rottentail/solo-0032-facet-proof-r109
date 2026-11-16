import argparse
import json
from pathlib import Path
import struct
import tempfile
import shutil
from .application import Application
from .errors import Fault, require
from .planner import build_plan
from .routing import route_request, PREFIX

FLOWS = ["mesh-intake", "mesh-retirement", "assembly-edit", "lens-edit", "seal-closure", "exposure-submit", "render-plates", "cancel-exposure", "retry-exposure", "verify-plate", "pixel-pick", "compare-plates", "proof-decision", "quarantine-plate", "recover-lease", "export-bundle", "reserve-capacity", "settle-capacity", "resize-capacity", "tile-progress", "offline-inspect", "backup-closure", "content-inspection", "motion-blur", "motion-pick", "motion-capacity", "motion-resume", "motion-bundle"]
MOTION_FLOWS = {"motion-blur", "motion-pick", "motion-capacity", "motion-resume", "motion-bundle"}

def commission(root, flow, output=None):
    app = Application(root)
    def call(method, route, data=None): return route_request(app, method, PREFIX + route, data)
    def rejected(method, route, data, code):
        try: call(method, route, data)
        except Fault as exc:
            require(exc.code == code, "commission", f"Expected {code}, got {exc.code}", 500)
            return exc.code
        raise Fault("commission", "Expected rejection", 500)
    if flow in MOTION_FLOWS:
        return motion_commission(app, call, rejected, flow, output)
    mesh_data = {"name":"斜翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]], "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]}
    mesh = call("POST", "meshes", mesh_data)
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"斜翼结构说明图", "lens":{"eye":[3,2,4]}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    result = {"workflow":flow}
    if flow == "mesh-intake":
        result["rejected"] = rejected("POST", "meshes", {**mesh_data, "faces":[[0,0,1]]}, "face")
        return result
    if flow == "mesh-retirement":
        call("POST", "meshes/"+mesh["id"]+"/retire", {"revision":1})
        result["rejected"] = rejected("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2}, "retired")
        return result
    if flow in ("assembly-edit", "lens-edit"):
        changes = {"name":"更新说明图"} if flow == "assembly-edit" else {"lens":{"projection":"orthographic"}}
        edited = call("PUT", "assemblies/"+assembly["id"], {"revision":2, **changes})
        result.update(revision=edited["revision"], rejected=rejected("PUT", "assemblies/"+assembly["id"], {"revision":2, "name":"过期"}, "stale_revision"))
        return result
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    if flow == "seal-closure":
        call("PUT", "pigments/"+pigment["id"], {"revision":1, "name":"改色", "color":[1,0,0]})
        require(call("GET", "seals/"+seal["id"]) == seal, "commission", "Seal changed", 500)
        result["rejected"] = rejected("PUT", "assemblies/"+assembly["id"], {"revision":3, "name":"改稿"}, "sealed")
        return result
    request = {"key":"commission", "width":96, "height":96, "samples":2}
    if flow in {'reserve-capacity', 'settle-capacity', 'resize-capacity'}:
        request.update(width=16, height=16, samples=1)
        call('PUT', 'resources', {'capacity':256, 'revision':1})
        exposure = call('POST', 'seals/'+seal['id']+'/exposures', request)
        result['reserved'] = call('GET', 'resources')['reserved']
        require(result['reserved'] == 256, 'commission', 'Reservation missing', 500)
        result['rejected'] = rejected('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'too-many'}, 'capacity_reserved')
        if flow == 'reserve-capacity':
            call('POST', 'exposures/'+exposure['id']+'/cancel')
        elif flow == 'settle-capacity':
            claim = app.exposures.claim()
            call('POST', 'exposures/'+exposure['id']+'/cancel')
            require(call('GET', 'resources')['reserved'] == 256, 'commission', 'Accepted cancellation released running work', 500)
            app.exposures.fail(exposure['id'], claim['token'], 'cancel_checkpoint')
        else:
            call('PUT', 'resources', {'capacity':512, 'revision':2})
            second = call('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'second'})
            policy = call('PUT', 'resources', {'capacity':256, 'revision':3})
            require(policy['overcommitted'] == 256, 'commission', 'Prior promises were erased', 500)
            rejected('PUT', 'resources', {'capacity':512,'revision':3}, 'stale_revision')
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Promised work lost',500)
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Second promise lost',500)
            result['settled'] = second['id']
        require(call('GET', 'resources')['reserved'] == 0, 'commission', 'Settled work leaked capacity', 500)
        result['capacity'] = call('GET', 'resources')
        return result
    exposure = call("POST", "seals/"+seal["id"]+"/exposures", request)
    eroute = "exposures/"+exposure["id"]
    if flow == 'tile-progress':
        require(not call('GET', eroute+'/progress')['planned'], 'commission', 'Queued work claims completed pixels', 500)
    if flow == "exposure-submit":
        require(call("POST", "seals/"+seal["id"]+"/exposures", request)["id"] == exposure["id"], "commission", "Idempotency mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures", {**request, "width":128}, "key_conflict")
        return result
    if flow == "cancel-exposure":
        result["state"] = call("POST", eroute+"/cancel")["state"]
        require(call("POST", "worker/step")["state"] == "idle", "commission", "Cancelled work claimed", 500)
        result["rejected"] = rejected("POST", eroute+"/retry", {}, "state")
        return result
    if flow in ("retry-exposure", "recover-lease"):
        claimed = app.exposures.claim(lease_seconds=-1 if flow == "recover-lease" else 120)
        if flow == "retry-exposure":
            app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
            call("POST", eroute+"/retry")
        else:
            require(exposure["id"] in call("POST", "worker/recover")["recovered"], "commission", "Recovery failed", 500)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    route = "plates/"+plate["id"]
    require(plate["payload"]["statistics"]["covered_pixels"] > 0, "commission", "Empty image", 500)
    result["statistics"] = plate["payload"]["statistics"]
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"): (Path(output)/name).write_bytes(call("GET", route+"/passes/"+name)[0])
    if flow == 'offline-inspect':
        from facetreader import validate_bundle
        bundle = call('GET', route+'/bundle')[0]
        report = validate_bundle(bundle)
        require(report['valid'], 'commission', str(report), 500)
        damaged = validate_bundle(bundle[:-20])
        require(not damaged['valid'], 'commission', 'Truncated package was accepted', 500)
        result['offline'] = report['summary']
        result['rejected'] = damaged['error']
    elif flow == 'backup-closure':
        proof = call('POST', route+'/proofs', {'note':'备份前核验'})
        call('POST', 'proofs/'+proof['id']+'/decide', {'state':'accepted','note':'通道完整'})
        backup = call('POST', 'maintenance/backups')
        op = backup['operation_id']
        require(call('GET', 'maintenance/backups/'+op)['state']=='running','commission','Missing running backup',500)
        rejected('GET', 'maintenance/backups/'+op+'/verify', {}, 'backup_incomplete')
        completed = call('POST', 'maintenance/backups/'+op+'/resume')
        require(completed['state']=='succeeded','commission','Backup was not published',500)
        call('POST', route+'/quarantine', {'reason':'备份时点后的撤回'})
        require(call('GET', 'maintenance/backups/'+op+'/verify')['valid'],'commission','Immutable backup verification failed',500)
        restore_root = Path(root)/'restore-check'
        shutil.copytree(completed['data_directory'], restore_root)
        restored = Application(restore_root)
        require(restored.plates.verify(plate['id'])['valid'] and restored.proofs.get(proof['id'])['state']=='accepted',
                'commission','Restored snapshot mixed later review facts',500)
        require(restored.bundles.export(plate['id']), 'commission', 'Restored export failed', 500)
        result['backup'] = {'state':completed['state'],'references':len(completed['manifest']['closure']['references'])}
        result['rejected'] = rejected('GET', route+'/bundle', {}, 'plate_state')
    elif flow == 'content-inspection':
        result['inspection'] = call('GET', 'maintenance/inspect')
        result['rejected'] = rejected('GET', 'maintenance/backups/unknown', {}, 'backup_id')
    elif flow == 'tile-progress':
        progress = call('GET', eroute+'/progress')
        require(progress['complete'] and progress['committed_tiles'] == 9 and progress['state'] == 'succeeded',
                'commission', 'Persisted progress does not match the completed exposure', 500)
        result['progress'] = progress
        result['verification'] = call('POST', route+'/verify')
    elif flow == "verify-plate": result["verification"] = call("POST", route+"/verify")
    elif flow == "pixel-pick":
        result["pick"] = call("GET", route+"/pick?x=48&y=48")
        require(result["pick"]["object"] is not None, "commission", "Center object missing", 500)
    elif flow == "compare-plates":
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"second"})
        second = call("POST", "worker/step")["plate"]
        result["comparison"] = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(result["comparison"]["color_changed"] == 0, "commission", "Determinism failed", 500)
    elif flow in ("proof-decision", "quarantine-plate"):
        proof = call("POST", route+"/proofs", {"note":"检查结构轮廓"})
        result["proof"] = call("POST", "proofs/"+proof["id"]+"/decide", {"state":"accepted", "note":"通过"})["state"]
        if flow == "quarantine-plate":
            call("POST", route+"/quarantine", {"reason":"撤回"})
            require(call("GET", "proofs/"+proof["id"])["state"] == "invalidated", "commission", "Proof remains accepted", 500)
            call("POST", route+"/purge")
            result["rejected"] = rejected("GET", route+"/bundle", {}, "plate_state")
    elif flow == "export-bundle":
        bundle = call("GET", route+"/bundle")[0]
        require(bundle == call("GET", route+"/bundle")[0], "commission", "Bundle changed", 500)
        result["bundle_bytes"] = len(bundle)
    if "rejected" not in result:
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
    return result

def motion_scene(call):
    backdrop = call("POST", "meshes", {"name":"背景板", "vertices":[[-3,-2,-.5],[3,-2,-.5],[3,2,-.5],[-3,2,-.5]],
                                       "faces":[[0,1,2],[0,2,3]]})
    shuttle = call("POST", "meshes", {"name":"滑块", "vertices":[[-.4,-.4,0],[.4,-.4,0],[.4,.4,0],[-.4,.4,0]],
                                      "faces":[[0,1,2],[0,2,3]]})
    teal = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    red = call("POST", "pigments", {"name":"信号红", "color":[.8,.2,.15], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"运动拖影说明图",
                                           "lens":{"eye":[0,0,6], "projection":"orthographic", "span":6}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[
        {"key":"backdrop", "mesh_id":backdrop["id"], "pigment_id":teal["id"]},
        {"key":"shuttle", "mesh_id":shuttle["id"], "pigment_id":red["id"],
         "transform":{"translation":[-2.5,0,0]}, "motion":{"translation":[5,0,0]}}]})
    return call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})


def motion_commission(app, call, rejected, flow, output):
    seal = motion_scene(call)
    result = {"workflow": flow}
    if flow == "motion-capacity":
        call("PUT", "resources", {"capacity":2048, "revision":1})
        exposure = call("POST", "seals/"+seal["id"]+"/exposures",
                        {"key":"motion-quota", "width":16, "height":16, "samples":1,
                         "shutter":{"open":0, "close":1, "samples":8}})
        require(exposure["request"]["temporal"]["slices"] == 8, "commission", "Temporal resolution missing", 500)
        require(exposure["allocation"]["units"] == 16*16*8, "commission", "Reservation ignores time slices", 500)
        require(call("GET", "resources")["reserved"] == 2048, "commission", "Reserved units mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures",
                                      {"key":"motion-overflow", "width":16, "height":16, "samples":1,
                                       "shutter":{"open":0, "close":1, "samples":8}}, "capacity_reserved")
        call("POST", "exposures/"+exposure["id"]+"/cancel")
        require(call("GET", "resources")["reserved"] == 0, "commission", "Cancelled reservation leaked", 500)
        result["capacity"] = call("GET", "resources")
        return result
    if flow == "motion-resume":
        exposure = call("POST", "seals/"+seal["id"]+"/exposures",
                        {"key":"motion-resume", "width":96, "height":96, "samples":1,
                         "shutter":{"open":0, "close":1, "samples":4}})
        claimed = app.exposures.claim()
        require(claimed["id"] == exposure["id"], "commission", "Claim mismatch", 500)
        with app.db.unit_of_work() as conn:
            seal_payload = app.db.get(conn, claimed["seal_id"], "seal")["payload"]
        plan = build_plan(seal_payload, claimed["request"])
        engine = app.worker.tiles
        frame, records = engine.store.prepare(claimed, plan, engine.tile_size)
        payload = engine.render_window(plan, lambda: None, tuple(records[0]["bounds"]), frame["fingerprint"])
        engine.store.commit(claimed, records[0]["ordinal"], payload)
        app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
        call("POST", "exposures/"+exposure["id"]+"/retry")
        rendered = call("POST", "worker/step")
        require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
        events = call("GET", "events/"+exposure["id"]+"/history")["items"]
        commits = [item for item in events if item["action"] == "tile_committed"
                   and item["payload"]["ordinal"] == records[0]["ordinal"]]
        require(len(commits) == 1, "commission", "Committed tile was counted again after interruption", 500)
        plate = rendered["plate"]
        require(plate["payload"]["format"] == "facet-plate-2", "commission", "Motion plate format missing", 500)
        first_fingerprint = call("GET", "exposures/"+exposure["id"]+"/progress")["fingerprint"]
        retimed = call("POST", "seals/"+seal["id"]+"/exposures",
                       {"key":"motion-retimed", "width":96, "height":96, "samples":1,
                        "shutter":{"open":0, "close":1, "samples":8}})
        require(call("POST", "worker/step")["state"] == "succeeded", "commission", "Retimed render failed", 500)
        second_fingerprint = call("GET", "exposures/"+retimed["id"]+"/progress")["fingerprint"]
        require(first_fingerprint != second_fingerprint,
                "commission", "Different timing shares tile identity", 500)
        result["resume"] = {"reused_tile_commits": len(commits), "fingerprints_differ": True}
        result["statistics"] = plate["payload"]["statistics"]
        return result
    if flow == "motion-blur":
        preview = call("POST", "seals/"+seal["id"]+"/exposures",
                       {"key":"preview", "width":64, "height":64, "samples":1,
                        "shutter":{"open":0, "close":1, "samples":2}})
        formal = call("POST", "seals/"+seal["id"]+"/exposures",
                      {"key":"formal", "width":64, "height":64, "samples":1,
                       "shutter":{"open":0, "close":1, "samples":16}})
        require(preview["request"]["temporal"] == {"open":0.0, "close":1.0, "requested_slices":2,
                                                   "slices":2, "motion":True},
                "commission", "Frozen time meaning missing from the exposure", 500)
        first, second = call("POST", "worker/step"), call("POST", "worker/step")
        require(first["state"] == second["state"] == "succeeded", "commission", "Motion render failed", 500)
        preview_payload, formal_payload = first["plate"]["payload"], second["plate"]["payload"]
        require(preview_payload["format"] == formal_payload["format"] == "facet-plate-2",
                "commission", "Motion plates must use the temporal format", 500)
        preview_loss = preview_payload["statistics"]["temporal_loss"]
        formal_loss = formal_payload["statistics"]["temporal_loss"]
        require(preview_loss["max"] > 0 and formal_loss["max"] > 0,
                "commission", "Approximation loss is not reported", 500)
        require(formal_loss["max"] < preview_loss["max"],
                "commission", "Finer integration did not reduce approximation loss", 500)
        require(formal_payload["temporal"]["slices"] == 16 and preview_payload["temporal"]["slices"] == 2,
                "commission", "Slice counts do not match the chosen integration", 500)
        require(formal_payload["temporal"]["loss"] == formal_loss,
                "commission", "Temporal block disagrees with statistics", 500)
        call("POST", "seals/"+seal["id"]+"/exposures",
             {"key":"instant", "width":64, "height":64, "samples":1})
        instant = call("POST", "worker/step")
        require(instant["plate"]["payload"]["format"] == "facet-plate-1",
                "commission", "Single-instant render must keep the static format", 500)
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            for name in ("color.png", "depth.png", "object.png"):
                (Path(output)/name).write_bytes(call("GET", "plates/"+second["plate"]["id"]+"/passes/"+name)[0])
        result["loss"] = {"preview": preview_loss, "formal": formal_loss}
        result["statistics"] = formal_payload["statistics"]
        return result
    if flow == "motion-pick":
        call("POST", "seals/"+seal["id"]+"/exposures",
             {"key":"pick", "width":64, "height":64, "samples":1,
              "shutter":{"open":0, "close":1, "samples":8}})
        plate = call("POST", "worker/step")["plate"]
        route = "plates/"+plate["id"]
        dominant_raw = call("GET", route+"/passes/object.u32")[0]
        latest_raw = call("GET", route+"/passes/latest.u32")[0]
        count = 64 * 64
        dominant_ids = struct.unpack("<%dI" % count, dominant_raw)
        latest_ids = struct.unpack("<%dI" % count, latest_raw)
        differing = [offset for offset in range(count) if dominant_ids[offset] != latest_ids[offset]]
        require(differing, "commission", "Dominant and latest never differ in the smear", 500)
        x, y = differing[0] % 64, differing[0] // 64
        dominant = call("GET", route+"/pick?x=%d&y=%d" % (x, y))
        latest = call("GET", route+"/pick?x=%d&y=%d&mode=latest" % (x, y))
        require(dominant["mode"] == "dominant" and latest["mode"] == "latest",
                "commission", "Pick mode is not echoed", 500)
        require(dominant["question"] != latest["question"],
                "commission", "Pick does not say which question it answers", 500)
        require(dominant["object"]["key"] != latest["object"]["key"],
                "commission", "Pick modes located the same part", 500)
        call("POST", "seals/"+seal["id"]+"/exposures",
             {"key":"still", "width":64, "height":64, "samples":1})
        still = call("POST", "worker/step")["plate"]
        picked = call("GET", "plates/"+still["id"]+"/pick?x=32&y=32")
        require("mode" not in picked and picked["object"] is not None,
                "commission", "Static pick changed", 500)
        result["rejected"] = rejected("GET", "plates/"+still["id"]+"/pick?x=32&y=32&mode=latest", {}, "pick_mode")
        result["pick"] = {"dominant": dominant, "latest": latest}
        return result
    if flow == "motion-bundle":
        call("POST", "seals/"+seal["id"]+"/exposures",
             {"key":"bundle", "width":64, "height":64, "samples":1,
              "shutter":{"open":0, "close":1, "samples":8}})
        plate = call("POST", "worker/step")["plate"]
        bundle = call("GET", "plates/"+plate["id"]+"/bundle")[0]
        from facetreader import validate_bundle
        report = validate_bundle(bundle)
        require(report["valid"], "commission", str(report), 500)
        require(report["format"] == "facet-plate-2", "commission", "Offline format mismatch", 500)
        require(report["exposure"]["slices"] == 8 and report["exposure"]["integration"] == "midpoint_rule",
                "commission", "Offline bundle lost the exposure choices", 500)
        require(report["attribution"]["latest.u32"] == "most_recently_visible_surface",
                "commission", "Offline bundle lost the attribution meaning", 500)
        require(report["summary"]["time_slices"] == 8, "commission", "Offline summary lost the slices", 500)
        damaged = validate_bundle(bundle[:-20])
        require(not damaged["valid"], "commission", "Truncated package was accepted", 500)
        result["offline"] = {"exposure": report["exposure"], "attribution": report["attribution"]}
        result["rejected"] = damaged["error"]
        return result
    raise Fault("commission", "Unknown motion flow", 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=FLOWS, default="render-plates")
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="facetproof-") as root:
        print(json.dumps(commission(root, args.flow, args.output), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()

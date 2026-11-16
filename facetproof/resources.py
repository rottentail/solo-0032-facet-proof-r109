"""Durable ownership of high-sample canvas capacity, independent of workers."""
from .errors import require
from .values import integer, now


def canvas_units(request):
    """High-sample pixels times shutter slices: the real rasterization work."""
    temporal = request.get('temporal') or {}
    return request['width'] * request['height'] * request['samples']**2 * temporal.get('slices', 1)


class ResourceBudget:
    def __init__(self, db):
        self.db = db
        with db.unit_of_work() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS sample_policy (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                capacity INTEGER NOT NULL, revision INTEGER NOT NULL)""")
            first_policy = conn.execute("INSERT OR IGNORE INTO sample_policy VALUES (1,4194304,1)").rowcount == 1
            conn.execute("""CREATE TABLE IF NOT EXISTS sample_allocations (
                exposure_id TEXT PRIMARY KEY, units INTEGER NOT NULL,
                generation INTEGER NOT NULL, active INTEGER NOT NULL,
                reserved REAL NOT NULL, released REAL, release_reason TEXT)""")
            if first_policy:
                from .values import parse_json
                # These requests were accepted before quotas existed. Record
                # every prior promise, including temporary overcommit, rather
                # than retroactively rejecting it under the new policy.
                for row in conn.execute("SELECT id,request FROM exposures WHERE state IN ('queued','running') ORDER BY created,id").fetchall():
                    request = parse_json(row['request'])
                    self._record(conn, row['id'], canvas_units(request), 1, 'capacity_adopted')

    def _snapshot(self, conn):
        policy = dict(conn.execute("SELECT capacity,revision FROM sample_policy WHERE singleton=1").fetchone())
        used = conn.execute("SELECT COALESCE(SUM(units),0) FROM sample_allocations WHERE active=1").fetchone()[0]
        return {**policy, "reserved": used, "available": max(0, policy['capacity']-used),
                "overcommitted": max(0, used-policy['capacity']),
                "unit": "high_sample_pixel_time_slice", "physical_memory_limit": False}

    def snapshot(self):
        with self.db.unit_of_work() as conn:
            return self._snapshot(conn)

    def revise(self, capacity, revision):
        capacity = integer(capacity, 'capacity', 256, 2**40)
        revision = integer(revision, 'revision', 1, 2**63-2)
        with self.db.unit_of_work() as conn:
            changed = conn.execute("UPDATE sample_policy SET capacity=?,revision=revision+1 WHERE singleton=1 AND revision=?",
                                   (capacity, revision)).rowcount
            require(changed == 1, 'stale_revision', 'Resource policy changed', 409)
            # Existing promises remain valid even after a capacity reduction.
            self.db.event(conn, 'sample-policy', 'capacity_revised', {'capacity': capacity, 'revision': revision+1})
            return self._snapshot(conn)

    def get(self, conn, exposure_id):
        row = conn.execute("SELECT * FROM sample_allocations WHERE exposure_id=?", (exposure_id,)).fetchone()
        return dict(row) if row else None

    def require_allocation(self, conn, exposure):
        allocation = self.get(conn, exposure['id'])
        units = canvas_units(exposure['request'])
        require(allocation is not None and allocation['active'] == 1 and allocation['units'] == units,
                'allocation_missing', 'Exposure has no matching active canvas allocation', 409)
        return allocation

    def _record(self, conn, exposure_id, units, generation, action):
        conn.execute("""INSERT INTO sample_allocations VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(exposure_id) DO UPDATE SET units=excluded.units,
            generation=excluded.generation,active=1,reserved=excluded.reserved,
            released=NULL,release_reason=NULL""",
            (exposure_id, units, generation, 1, now(), None, None))
        self.db.event(conn, exposure_id, action, {'units': units, 'generation': generation})

    def reserve(self, conn, exposure_id, request):
        units = canvas_units(request)
        old = self.get(conn, exposure_id)
        if old and old['active']:
            require(old['units'] == units, 'allocation', 'Exposure allocation cannot change its input', 409)
            return old
        policy = self._snapshot(conn)
        require(units <= policy['available'], 'capacity_reserved', 'Insufficient unreserved canvas capacity', 429)
        generation = old['generation']+1 if old else 1
        self._record(conn, exposure_id, units, generation, 'capacity_reserved')
        return self.get(conn, exposure_id)

    def release(self, conn, exposure_id, reason):
        # Callers first settle the exposure inside this same SQL unit. This
        # prevents cancellation acceptance or a stale attempt freeing a promise.
        row = conn.execute("SELECT state FROM exposures WHERE id=?", (exposure_id,)).fetchone()
        require(row is not None and row['state'] in {'succeeded', 'failed', 'cancelled'},
                'allocation_state', 'Only settled exposures can release capacity', 409)
        changed = conn.execute("UPDATE sample_allocations SET active=0,released=?,release_reason=? WHERE exposure_id=? AND active=1",
                               (now(), reason, exposure_id)).rowcount
        if changed:
            self.db.event(conn, exposure_id, 'capacity_released', {'reason': reason})
        return self.get(conn, exposure_id)

    def reconcile(self):
        """Reconcile legacy directories without inventing successful results."""
        with self.db.unit_of_work() as conn:
            from .values import parse_json
            rows = conn.execute("SELECT id,state,request FROM exposures ORDER BY created,id").fetchall()
            adopted, released, waiting = [], [], []
            for row in rows:
                old = self.get(conn, row['id'])
                if row['state'] in {'succeeded', 'failed', 'cancelled'}:
                    if old and old['active']:
                        self.release(conn, row['id'], 'reconciled_terminal')
                        released.append(row['id'])
                elif not old or not old['active']:
                    request = parse_json(row['request'])
                    if canvas_units(request) <= self._snapshot(conn)['available']:
                        self.reserve(conn, row['id'], request)
                        adopted.append(row['id'])
                    else:
                        # Keep unaccounted work queued until an operator can
                        # reconcile its allocation after capacity is available.
                        waiting.append(row['id'])
            return {'adopted': adopted, 'released': released, 'waiting': waiting,
                    'capacity': self._snapshot(conn)}

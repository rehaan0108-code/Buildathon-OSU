"""Ground navigation and cover selection for Mech Royale."""
from array import array
from collections import defaultdict
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count
import json
from math import atan2, ceil, cos, dist, floor, hypot, pi, radians, sin, tan
from pathlib import Path
import sys
from time import perf_counter


GRID = 6
BUCKET = 24
DIRECTIONS = [(1, 0), (-1, 0), (0, 1), (0, -1),
              (1, 1), (1, -1), (-1, 1), (-1, -1)]


def edges(ring):
    points = list(zip(ring[::2], ring[1::2]))
    return list(zip(points, points[1:] + points[:1]))


def ring_metrics(x, y, segments):
    inside = False
    nearest = float("inf")
    for (ax, ay), (bx, by) in segments:
        dx, dy = bx - ax, by - ay
        if (ay > y) != (by > y) and x < ax + dx * (y - ay) / dy:
            inside = not inside
        length_sq = dx * dx + dy * dy
        t = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / length_sq)) if length_sq else 0
        nearest = min(nearest, hypot(x - ax - t * dx, y - ay - t * dy))
    return inside, nearest


class Arena:
    def __init__(self, path, mech):
        path = Path(path) / "v2"
        world = json.loads((path / "world.json").read_text(encoding="utf-8"))
        terrain = world["terrain"]
        self.x0, self.y0 = terrain["minX"], terrain["minY"]
        self.nx, self.ny = terrain["nx"], terrain["ny"]
        self.spacing, self.scale = terrain["step"], terrain["scale"]
        self.data = array("h", (path / terrain["file"]).read_bytes())
        if sys.byteorder != "little":
            self.data.byteswap()
        if len(self.data) != self.nx * self.ny:
            raise ValueError("Unexpected terrain size")
        self.slope = tan(radians(mech.get("max_slope_deg", 50)))
        self.step = mech.get("step_up", 1.2)
        self.buckets = defaultdict(list)
        for prism in world["prisms"]:
            ring = prism["ring"]
            xs, ys = ring[::2], ring[1::2]
            box = min(xs), min(ys), max(xs), max(ys)
            solid = (box, prism["base"], prism["top"], edges(ring),
                     [edges(hole) for hole in prism["holes"]])
            for i in range(floor(box[0] / BUCKET), floor(box[2] / BUCKET) + 1):
                for j in range(floor(box[1] / BUCKET), floor(box[3] / BUCKET) + 1):
                    self.buckets[i, j].append(solid)

    def height(self, x, y):
        gx = min(self.nx - 1, max(0, (x - self.x0) / self.spacing))
        gy = min(self.ny - 1, max(0, (y - self.y0) / self.spacing))
        i, j = min(int(gx), self.nx - 2), min(int(gy), self.ny - 2)
        u, v = gx - i, gy - j
        k = j * self.nx + i
        a = self.data[k] * (1 - u) + self.data[k + 1] * u
        k += self.nx
        b = self.data[k] * (1 - u) + self.data[k + 1] * u
        return (a * (1 - v) + b * v) * self.scale

    @lru_cache(maxsize=20000)
    def clearance(self, x, y, height, limit=12, feet=None):
        z = self.height(x, y) if feet is None else feet
        nearest = min(limit, x - self.x0, y - self.y0,
                      self.x0 + (self.nx - 1) * self.spacing - x,
                      self.y0 + (self.ny - 1) * self.spacing - y)
        if nearest <= 0:
            return 0
        seen = set()
        for i in range(floor((x - limit) / BUCKET), floor((x + limit) / BUCKET) + 1):
            for j in range(floor((y - limit) / BUCKET), floor((y + limit) / BUCKET) + 1):
                for solid in self.buckets.get((i, j), ()):
                    if id(solid) in seen:
                        continue
                    seen.add(id(solid))
                    (x0, y0, x1, y1), base, top, outer, holes = solid
                    if not (z < top and z + height >= base):
                        continue
                    if x < x0 - nearest or x > x1 + nearest or y < y0 - nearest or y > y1 + nearest:
                        continue
                    inside, distance = ring_metrics(x, y, outer)
                    if inside:
                        for hole in holes:
                            in_hole, hole_distance = ring_metrics(x, y, hole)
                            if in_hole:
                                distance = hole_distance
                                break
                        else:
                            return 0
                    nearest = min(nearest, distance)
        return nearest

    def segment(self, start, end, radius, height, escape=False):
        length = dist(start, end)
        steps = max(1, ceil(length / 1.0))
        previous = self.height(*start)
        narrowest = 12
        blocked = escape and self.clearance(*start, height) < radius
        escape_clearance = self.clearance(*start, height) if blocked else 0
        for i in range(1, steps + 1):
            t = i / steps
            x, y = (start[k] + t * (end[k] - start[k]) for k in range(2))
            z = self.height(x, y)
            climb = z - previous
            if climb > min(self.step, self.slope * length / steps) + 0.05:
                return None
            clearance = self.clearance(x, y, height)
            if clearance < radius:
                if not blocked or clearance + 0.01 < escape_clearance:
                    return None
                escape_clearance = clearance
            else:
                blocked = False
            previous = z
            narrowest = min(narrowest, clearance)
        return None if blocked else narrowest


    def solid_at(self, x, y, z):
        if z <= self.height(x, y):
            return True
        for box, base, top, outer, holes in self.buckets.get((floor(x / BUCKET), floor(y / BUCKET)), ()):
            if base <= z <= top and box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                if ring_metrics(x, y, outer)[0] and not any(ring_metrics(x, y, hole)[0] for hole in holes):
                    return True
        return False

    def shot_blocked(self, source, target, spacing=1.0):
        # Approximate straight projectile cover. Thin geometry can fall between samples.
        steps = max(1, ceil(dist(source, target) / spacing))
        for i in range(1, steps):
            t = i / steps
            if self.solid_at(*(a + t * (b - a) for a, b in zip(source, target))):
                return True
        return False


class Navigation:
    def __init__(self, hello):
        cfg = hello["config"]
        self.mech = cfg["mech"]
        self.mortar_gravity = cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22)
        self.threat_key = ()
        self.growth = cfg["cubes"].get("size_per_cube", 0.05)
        self.budget = min(0.018, cfg["bot"]["tick_budget_ms"] / 2000)
        self.arena = Arena(hello["map"]["dir"], self.mech)
        self.avoid = None
        self.route = []
        self.last_plan = -float("inf")
        self.progress = None
        self.key = None

    def retreat(self, obs, enemies):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        if not me["grounded"]:
            return None
        x, y, z = me["pos"]
        start = x, y
        growth = 1 + me["cubes"] * self.growth
        radius = self.mech.get("radius", 2.5) * growth + 0.35
        height = self.mech.get("height", 9) * growth
        speed = max(1, me["stats"].get("walk_speed", 11.7) * 0.75)
        if self.zone_margin(zone, now, start, now + 3) < radius + 6:
            return None
        sources = [(ex, ey, ez + 7 * (1 + enemy.get("cubes", 0) * self.growth))
                   for enemy in enemies for ex, ey, ez in [enemy["pos"]]]
        away = atan2(y - sources[0][1], x - sources[0][0])
        deadline = perf_counter() + 0.008
        fallback = None
        for distance in (6, 12, 18):
            for offset in (0, 0.65, -0.65, 1.3, -1.3, 2.25, -2.25):
                if perf_counter() >= deadline:
                    return fallback
                yaw = away + offset
                movement = cos(yaw), sin(yaw)
                point = x + movement[0] * distance, y + movement[1] * distance
                if not self.zone_leg(zone, now, start, point, now, now + distance / speed, radius + 4):
                    continue
                if self.arena.segment(start, point, radius, height, escape=True) is None:
                    continue
                if fallback is None and abs(offset) <= 1.3:
                    fallback = list(movement)
                floor_height = self.arena.height(*point)
                if all(self.arena.shot_blocked(source, (*point, floor_height + height * level), spacing=2)
                       for source in sources for level in (0.5, 0.85)):
                    return list(movement)
        return fallback

    def movement(self, obs):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        start = tuple(me["pos"][:2])
        center = tuple(zone["next_center"])
        if not me["grounded"] or dist(start, center) < 2:
            return None
        growth = 1 + me["cubes"] * self.growth
        radius = self.mech.get("radius", 2.5) * growth + 0.35
        height = self.mech.get("height", 9) * growth + 0.15
        speed = max(1, me["stats"].get("walk_speed", 11.7) * 0.75)
        key = (*center, zone["next_radius"], me["cubes"])
        if key != self.key:
            self.route.clear()
            self.avoid = None
            self.key = key
        while self.route and dist(start, self.route[0]) < 1.8:
            self.route.pop(0)
        if not self.progress or dist(start, self.progress[0]) > 2 or not self.route:
            self.progress = start, now
        elif now - self.progress[1] > 1.4:
            self.avoid = self.route[0]
            self.route.clear()
            self.progress = start, now
        if self.route and not self.zone_leg(zone, now, start, self.route[0], now,
                                            now + dist(start, self.route[0]) / speed, radius):
            self.route.clear()
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: dist(start, m["pos"][:2]))[:2]
        threat_key = tuple((m["id"], *(round(v / 10) for v in m["pos"])) for m in enemies)
        if not self.route or now - self.last_plan >= 1.0 or (
                threat_key != self.threat_key and now - self.last_plan >= 0.25):
            sources = tuple((x, y, z + 7 * (1 + m.get("cubes", 0) * self.growth))
                            for m in enemies for x, y, z in [m["pos"]])
            urgent = self.zone_margin(zone, now, start, now + 3) < radius + 8
            route = self.plan(start, center, radius, height, 0, perf_counter() + self.budget,
                              zone, now, speed, sources, 0.3 if urgent else 1.8)
            self.threat_key = threat_key
            if route:
                self.route = [point for point in route if dist(start, point) >= 1.8]
            self.last_plan = now
        if not self.route:
            return None
        point = self.route[0]
        if self.arena.segment(start, point, radius, height, escape=True) is None:
            self.route.clear()
            return None
        distance = dist(start, point)
        return [(b - a) / max(4, distance) for a, b in zip(start, point)]

    def plan(self, start, target, radius, height, narrow_cost, deadline, zone, now, speed, threats=(), exposure_weight=0):
        queue = []
        sequence = count()
        origin = tuple(round(v / GRID) for v in start)
        parents, costs, arrivals = {}, {}, {}
        best, best_score = None, float("inf")

        @lru_cache(maxsize=2048)
        def exposure(point):
            if not threats:
                return 0
            x, y = point
            ground = self.arena.height(x, y)
            risk = 0
            for source in threats:
                if dist(source[:2], point) > 160:
                    continue
                for level in (0.5, 0.85):
                    if perf_counter() >= deadline:
                        return 1
                    if not self.arena.shot_blocked(source, (x, y, ground + level * height), spacing=3):
                        risk += 0.5
            return risk

        def push(node, parent, cost, arrival):
            if cost >= costs.get(node, float("inf")):
                return
            costs[node], parents[node], arrivals[node] = cost, parent, arrival
            point = node[0] * GRID, node[1] * GRID
            heappush(queue, (cost + dist(point, target), next(sequence), cost, node))

        for dx, dy in [(0, 0), *DIRECTIONS]:
            node = origin[0] + dx, origin[1] + dy
            point = node[0] * GRID, node[1] * GRID
            clearance = self.arena.segment(start, point, radius, height, escape=True)
            arrival = now + dist(start, point) / speed
            if clearance is not None and self.zone_leg(zone, now, start, point, now, arrival, radius):
                leg = dist(start, point)
                risk = exposure(point)
                push(node, None, leg * (1 + exposure_weight * risk) + narrow_cost * max(0, radius + 3 - clearance), arrival)
            if perf_counter() >= deadline:
                break

        expanded = 0
        while queue and expanded < 380 and perf_counter() < deadline:
            _, _, cost, node = heappop(queue)
            if cost != costs[node]:
                continue
            expanded += 1
            point = node[0] * GRID, node[1] * GRID
            remaining = dist(point, target)
            score = remaining + cost * 0.22
            if score < best_score:
                best, best_score = node, score
            if remaining <= GRID:
                best = node
                break
            for dx, dy in DIRECTIONS:
                if perf_counter() >= deadline:
                    break
                neighbor = node[0] + dx, node[1] + dy
                endpoint = neighbor[0] * GRID, neighbor[1] * GRID
                leg = dist(point, endpoint)
                if costs.get(neighbor, float("inf")) <= cost + leg:
                    continue
                arrival = arrivals[node] + leg / speed
                if not self.zone_leg(zone, now, point, endpoint, arrivals[node], arrival, radius):
                    continue
                clearance = self.arena.segment(point, endpoint, radius, height)
                if clearance is None:
                    continue
                pinch = max(0, radius + 3 - clearance)
                repeat = 30 if self.avoid and dist(endpoint, self.avoid) < 10 else 0
                midpoint = ((point[0] + endpoint[0]) / 2, (point[1] + endpoint[1]) / 2)
                risk = (exposure(point) + 2 * exposure(midpoint) + exposure(endpoint)) / 4
                push(neighbor, node, cost + leg * (1 + narrow_cost * pinch / 3 + exposure_weight * risk) + repeat, arrival)

        route = []
        while best is not None:
            route.append((best[0] * GRID, best[1] * GRID))
            best = parents[best]
        return list(reversed(route))

    @staticmethod
    def zone_margin(zone, now, point, at):
        start = max(now, zone["shrink_start"])
        end = zone["shrink_end"]
        fraction = min(1, max(0, (at - start) / (end - start))) if end > start else 0
        center = tuple(a + fraction * (b - a) for a, b in zip(zone["center"], zone["next_center"]))
        size = zone["radius"] + fraction * (zone["next_radius"] - zone["radius"])
        return size - dist(point, center)

    def zone_leg(self, zone, now, start, end, depart, arrival, radius):
        initial = self.zone_margin(zone, now, start, depart)
        minimum = min(initial, radius + 2)
        for at in (zone["shrink_start"], zone["shrink_end"], arrival):
            if depart < at <= arrival:
                fraction = (at - depart) / (arrival - depart)
                point = tuple(a + fraction * (b - a) for a, b in zip(start, end))
                if self.zone_margin(zone, now, point, at) < minimum - 0.01:
                    return False
        return True

    def defend_movement(self, obs, action, deadline):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        x, y, z = me["pos"]
        start = x, y
        scale = 1 + me["cubes"] * self.growth
        radius = self.mech.get("radius", 2.5) * scale
        height = self.mech.get("height", 9) * scale
        planned = tuple(action.get("move", (0, 0)))
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"
                          and dist(m["pos"], me["pos"]) < 85),
                         key=lambda enemy: dist(enemy["pos"], me["pos"]))[:2]
        sources = []
        for enemy in enemies:
            ex, ey, ez = enemy["pos"]
            sources.append((ex, ey, ez + 7 * (1 + enemy.get("cubes", 0) * self.growth)))

        # End projectile traces at solid terrain or structures.
        times = (0.08, 0.16, 0.24, 0.32, 0.40)
        projectiles = sorted(obs["projectiles"], key=lambda p: dist(p["pos"], me["pos"]))[:6]
        traces = []
        for projectile in projectiles:
            if perf_counter() >= deadline:
                break
            px, py, pz = projectile["pos"]
            vx, vy, vz = projectile["vel"]
            gravity = self.mortar_gravity if projectile["kind"] == "mortar" else 0
            previous = px, py, pz
            trace = []
            for t in times:
                at = px + vx * t, py + vy * t, pz + vz * t - 0.5 * gravity * t * t
                if self.arena.solid_at(*at) or self.arena.shot_blocked(previous, at, spacing=2):
                    break
                trace.append((t, previous, at))
                previous = at
            if trace:
                traces.append(trace)
        if not traces and (not sources or not me["grounded"]):
            return action

        horizontal_speed = me["stats"].get("walk_speed", 11.7)
        grounded = me["grounded"]
        vx, vy, vz = me["vel"]

        def predict(direction, t, boost=False):
            rate = me["stats"].get("boost_speed", horizontal_speed) if boost and grounded else horizontal_speed
            blend = min(1, t * (5 if grounded else 1.5))
            px = x + t * (vx * (1 - blend) + direction[0] * rate * blend)
            py = y + t * (vy * (1 - blend) + direction[1] * rate * blend)
            return px, py

        def shot_risk(direction, boost=False):
            risk = 0
            for trace in traces:
                for t, a, b in trace:
                    px, py = predict(direction, t - 0.04, boost)
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    frac = min(1, max(0, ((px - a[0]) * dx + (py - a[1]) * dy) / max(0.01, dx * dx + dy * dy)))
                    sx, sy = a[0] + frac * dx, a[1] + frac * dy
                    sz = a[2] + frac * (b[2] - a[2])
                    foot = self.arena.height(px, py) if grounded else z + vz * t - 11 * t * t
                    if hypot(sx - px, sy - py) < radius + 0.6 and foot - 0.5 <= sz <= foot + height + 0.5:
                        risk += 5 - t
                        break
            return risk

        def exposure(point):
            foot = self.arena.height(*point)
            return sum(not self.arena.shot_blocked(source, (*point, foot + level * height), spacing=2.5)
                       for source in sources for level in (0.5, 0.85))

        danger = shot_risk(planned, action.get("boost", False))
        endpoint = predict(planned, 0.40, action.get("boost", False))
        if perf_counter() >= deadline:
            return action
        base_exposure = exposure(endpoint) if grounded and sources else 0
        guarded = grounded and bool(sources) and self.zone_margin(zone, now, start, now + 4) >= radius + 8
        current_exposure = exposure(start) if guarded and perf_counter() < deadline else None
        leaves_cover = current_exposure is not None and max(base_exposure, exposure(predict(planned, 0.20, action.get("boost", False)))) > current_exposure
        if not danger and not base_exposure and not leaves_cover:
            return action

        best_score = danger * 10 + base_exposure * 2
        best = planned
        if leaves_cover:
            best = (0, 0)
            best_score = shot_risk(best) * 10 + current_exposure * 2 + 3
        heading = atan2(planned[1], planned[0]) if hypot(*planned) > 0.1 else atan2(
            zone["next_center"][1] - y, zone["next_center"][0] - x)
        for offset in (0.7, -0.7, 1.57, -1.57, 2.35, -2.35, 3.14):
            if perf_counter() >= deadline:
                break
            direction = cos(heading + offset), sin(heading + offset)
            point = predict(direction, 0.40, action.get("boost", False))
            if not self.zone_leg(zone, now, start, point, now, now + 0.65, radius + 2):
                continue
            if grounded and self.arena.segment(start, point, radius + 0.35, height, escape=True) is None:
                continue
            if not grounded and self.arena.clearance(*point, height, max(3, radius), feet=z + vz * 0.4 - 1.76) < radius:
                continue
            risk = shot_risk(direction, action.get("boost", False)) * 10
            if grounded and sources and perf_counter() < deadline:
                endpoint_exposure = exposure(point)
                if current_exposure is not None and max(endpoint_exposure, exposure(predict(direction, 0.20, action.get("boost", False)))) > current_exposure:
                    continue
                risk += endpoint_exposure * 2
            elif current_exposure is not None:
                break
            elif base_exposure:
                risk += base_exposure * 2
            score = risk + 0.65 + 0.4 * abs(offset)
            if score < best_score:
                best, best_score = direction, score
        if best != planned:
            action["move"] = list(best)
        return action

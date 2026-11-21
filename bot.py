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

import mechfight as mf

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
    def clearance(self, x, y, height, limit=12):
        z = self.height(x, y)
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

    def shot_blocked(self, source, target):
        # Approximate straight projectile cover. Thin geometry can fall between samples.
        steps = max(1, ceil(dist(source, target) / 1.0))
        for i in range(1, steps):
            t = i / steps
            if self.solid_at(*(a + t * (b - a) for a, b in zip(source, target))):
                return True
        return False


class Bot:
    name = "ground-survivor"

    def __init__(self, hello):
        cfg = hello["config"]
        self.mech = cfg["mech"]
        self.size_per_cube = cfg["cubes"].get("size_per_cube", 0.05)
        self.pickup_radius = cfg["loot"]["pickup_radius"]
        self.arena = Arena(hello["map"]["dir"], self.mech)
        self.budget = min(0.025, cfg["bot"]["tick_budget_ms"] / 2000)
        self.route = []
        self.last_plan = -float("inf")
        self.last_progress = None
        self.avoid = None
        self.zone_key = None
        self.cubes = None

    def plan(self, start, target, radius, height, narrow_cost, deadline):
        queue = []
        sequence = count()
        origin = tuple(round(v / GRID) for v in start)
        parents, costs = {}, {}
        best, best_score = None, float("inf")

        def push(node, parent, cost):
            if cost >= costs.get(node, float("inf")):
                return
            costs[node], parents[node] = cost, parent
            point = node[0] * GRID, node[1] * GRID
            heappush(queue, (cost + dist(point, target), next(sequence), cost, node))

        for dx, dy in [(0, 0), *DIRECTIONS]:
            node = origin[0] + dx, origin[1] + dy
            point = node[0] * GRID, node[1] * GRID
            clearance = self.arena.segment(start, point, radius, height, escape=True)
            if clearance is not None:
                push(node, None, dist(start, point) + narrow_cost * max(0, radius + 3 - clearance))
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
                clearance = self.arena.segment(point, endpoint, radius, height)
                if clearance is None:
                    continue
                pinch = max(0, radius + 3 - clearance)
                repeat = 30 if self.avoid and dist(endpoint, self.avoid) < 10 else 0
                push(neighbor, node, cost + leg * (1 + narrow_cost * pinch / 3) + repeat)

        route = []
        while best is not None:
            route.append((best[0] * GRID, best[1] * GRID))
            best = parents[best]
        return list(reversed(route))

    def destination(self, start, zone, radius, height, threat, deadline):
        center = tuple(zone["next_center"])
        safe_radius = max(0, zone["next_radius"] - radius - GRID)
        distance = dist(start, center)
        if distance > safe_radius:
            t = (distance - max(0, safe_radius - 24)) / distance
            return tuple(a + t * (b - a) for a, b in zip(start, center))
        if threat is None:
            return start
        tx, ty, tz = threat["pos"]
        source = tx, ty, tz + 7
        bearing = atan2(start[1] - ty, start[0] - tx)
        best, best_score = start, -float("inf")
        for step, offset in [(0, 0), (12, 0), (12, 1), (12, -1), (24, 0), (24, 1), (24, -1)]:
            if perf_counter() >= deadline:
                break
            yaw = bearing + offset * pi / 3
            x, y = start[0] + step * cos(yaw), start[1] + step * sin(yaw)
            if dist((x, y), center) > safe_radius or self.arena.clearance(x, y, height) < radius + 1:
                continue
            if self.arena.segment(start, (x, y), radius, height) is None:
                continue
            z = self.arena.height(x, y)
            blocked = 0
            for fraction in (0.25, 0.55, 0.85):
                if perf_counter() >= deadline:
                    break
                blocked += self.arena.shot_blocked(source, (x, y, z + height * fraction))
            score = blocked * 22 + dist((x, y), (tx, ty)) * 0.35 - step * 0.2
            if score > best_score:
                best, best_score = (x, y), score
        return best

    def act(self, obs):
        deadline = perf_counter() + self.budget
        me, zone = obs["self"], obs["zone"]
        x, y, z = me["pos"]
        start = x, y
        now = obs["t"]
        scale = 1 + me["cubes"] * self.size_per_cube
        radius = self.mech.get("radius", 2.5) * scale + 0.35
        height = self.mech.get("height", 9) * scale + 0.15
        action = {"tick": obs["tick"]}
        if obs["offer"]:
            choices = obs["offer"]["choices"]
            preferences = {"armor": 0, "health": 1, "speed": 2, "sensors": 3}
            action["pick"] = min(range(len(choices)), key=lambda i: preferences.get(choices[i]["id"], 4))

        if None in me["weapons"]:
            for pickup in obs["pickups"]:
                px, py, pz = pickup["pos"]
                if pickup["kind"] != "gun" and hypot(px - x, py - y) <= self.pickup_radius and abs(pz - z) <= 4:
                    action["take"] = pickup["id"]
                    break

        threat = min(obs["mechs"], key=lambda m: dist(m["pos"], me["pos"]), default=None)
        if threat is not None and dist(threat["pos"], me["pos"]) <= 110:
            tx, ty, tz = threat["pos"]
            action["aim"] = [atan2(ty - y, tx - x), atan2(tz - z, hypot(tx - x, ty - y))]
        else:
            threat = None

        zone_key = (*zone["next_center"], zone["next_radius"])
        changed = zone_key != self.zone_key or me["cubes"] != self.cubes
        if changed:
            self.route.clear()
        self.zone_key, self.cubes = zone_key, me["cubes"]

        while self.route and dist(start, self.route[0]) < 1.8:
            self.route.pop(0)
        if self.last_progress is None or dist(start, self.last_progress[0]) > 2 or not self.route:
            self.last_progress = (start, now)
        elif now - self.last_progress[1] > 1.4:
            self.avoid = self.route[0]
            self.route.clear()
            self.last_progress = (start, now)
            changed = True

        if changed or now - self.last_plan >= (0.5 if not self.route else 2):
            target = self.destination(start, zone, radius, height, threat, deadline - 0.008)
            outside = dist(start, zone["next_center"]) > zone["next_radius"]
            urgent = outside and (not me["in_zone"] or
                                  now + dist(start, target) / max(1, me["stats"].get("walk_speed", 10)) + 3 >= zone["shrink_end"])
            armed = all(w is not None for w in me["weapons"]) and me["hp"] >= 0.6 * me["max_hp"] and me["energy"] >= 35
            narrow_cost = 0.6 if urgent else 1.8 if armed else 4.0
            if dist(start, target) < 2:
                self.route.clear()
            else:
                route = self.plan(start, target, radius, height, narrow_cost, deadline)
                if route:
                    self.route = route
                    while self.route and dist(start, self.route[0]) < 1.8:
                        self.route.pop(0)
            self.last_plan = now

        if self.route and me["grounded"]:
            waypoint = self.route[0]
            dx, dy = waypoint[0] - x, waypoint[1] - y
            length = hypot(dx, dy)
            clearance = self.arena.segment(start, waypoint, radius, height, escape=True)
            if clearance is None:
                self.route.clear()
            elif length > 0:
                speed = min(1, max(0.35, length / 4))
                vx, vy, _ = me["vel"]
                if speed > 0.5 and vx * dx + vy * dy < -2:
                    speed = 0.5
                action["move"] = [speed * dx / length, speed * dy / length]
                action["boost"] = not me["in_zone"] and clearance > radius + 3 and length > 4
        return action


if __name__ == "__main__":
    mf.run(Bot)

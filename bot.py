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

    def building_between(self, source, target):
        steps = max(1, ceil(dist(source, target)))
        for i in range(1, steps):
            t = i / steps
            x, y, z = (a + t * (b - a) for a, b in zip(source, target))
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
    name = "cover-jumper"

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
        self.hop = None
        self.peek = None
        self.cube_goal = None
        self.cube_radius = cfg["cubes"]["pickup_radius"]
        self.last_hop = -float("inf")

    def plan(self, start, target, radius, height, narrow_cost, deadline, zone, now, speed):
        queue = []
        sequence = count()
        origin = tuple(round(v / GRID) for v in start)
        parents, costs, arrivals = {}, {}, {}
        best, best_score = None, float("inf")

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
                push(node, None, dist(start, point) + narrow_cost * max(0, radius + 3 - clearance), arrival)
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
                push(neighbor, node, cost + leg * (1 + narrow_cost * pinch / 3) + repeat, arrival)

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

    def destination(self, start, zone, radius, height, threat, deadline):
        center = tuple(zone["next_center"])
        inset = min(45, max(radius + GRID, zone["next_radius"] * 0.24))
        safe_radius = max(0, zone["next_radius"] - inset)
        distance = dist(start, center)
        if distance > safe_radius or (threat is None and distance < safe_radius - 24):
            dx, dy = ((start[0] - center[0]) / distance, (start[1] - center[1]) / distance) if distance else (1, 0)
            for fraction in (0.98, 0.85, 0.70, 0.50, 0):
                for offset in (0, 0.25, -0.25, 0.50, -0.50):
                    if perf_counter() >= deadline:
                        return center
                    c, s = cos(offset), sin(offset)
                    point = center[0] + safe_radius * fraction * (dx * c - dy * s), center[1] + safe_radius * fraction * (dx * s + dy * c)
                    if self.arena.clearance(*point, height) >= radius + 2:
                        return point
            return center
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

    def hop_route(self, me, zone, now, target, radius, height, deadline):
        """Screen short jets for overhead clearance and an in-zone touchdown."""
        x, y, z = me["pos"]
        config = self.mech
        gravity = config.get("gravity", 22)
        launch = me["stats"].get("jump_speed", config.get("jump_speed", 12))
        jet = me["stats"].get("jet_accel", config.get("jet_accel", 32))
        vmax = me["stats"].get("max_rise_speed", config.get("max_rise_speed", 20))
        air_accel = config.get("air_accel", 12)
        speed = me["stats"].get("walk_speed", 11.7)
        heading = atan2(target[1] - y, target[0] - x)
        best, best_score = None, -float("inf")
        for offset in (0, 0.65, -0.65, 1.25, -1.25):
            direction = cos(heading + offset), sin(heading + offset)
            px, py, pz = x, y, z
            vx, vy = me["vel"][:2]
            vz = launch
            margin = min(radius + 2, self.zone_margin(zone, now, (x, y), now))
            for tick in range(1, 61):
                if perf_counter() >= deadline:
                    return best
                dt = 0.05
                elapsed = tick * dt
                dx, dy = direction[0] * speed - vx, direction[1] * speed - vy
                change = min(1, air_accel * dt / max(0.01, hypot(dx, dy)))
                vx, vy = vx + dx * change, vy + dy * change
                px, py = px + vx * dt, py + vy * dt
                vz = min(vmax, vz + ((jet if 0.10 < elapsed <= 0.30 else 0) - gravity) * dt)
                pz += vz * dt
                ground = self.arena.height(px, py)
                if self.zone_margin(zone, now, (px, py), now + elapsed) < margin - 0.01:
                    break
                if vz > 0 and ground > pz:
                    break
                if self.arena.clearance(px, py, height, max(3, radius), feet=max(ground, pz)) < radius:
                    break
                if vz <= 0 and pz <= ground:
                    score = dist((x, y), target) - dist((px, py), target)
                    if score > max(2, best_score) and self.arena.clearance(px, py, height) >= radius + 0.5:
                        best, best_score = direction, score
                    break
        return best

    def gun_solution(self, me, threat, muzzle):
        gun = me["weapons"][0]
        if gun is None or gun["kind"] != "gun":
            return None
        stats = gun["stats"]
        target = (*threat["pos"][:2], threat["pos"][2] + 4.5)
        flight = dist(muzzle, target) / max(1, stats["speed"])
        target = tuple(p + v * flight for p, v in zip(target, threat["vel"]))
        dx, dy, dz = (b - a for a, b in zip(muzzle, target))
        pitch = atan2(dz, hypot(dx, dy))
        if dist(muzzle, target) > stats["range"] or not self.mech["min_pitch"] <= pitch <= self.mech["max_pitch"]:
            return None
        return [atan2(dy, dx), pitch], target

    def peek_height(self, me, threat, zone, now, radius, height, deadline):
        if not me["grounded"] or me["energy"] < 35 or me["hp"] < me["max_hp"] * 0.35:
            return None
        if now - self.last_hop < 4 or self.zone_margin(zone, now, me["pos"][:2], now + 3) < radius + 10:
            return None
        if hypot(*me["vel"][:2]) > 2:
            return None
        x, y, z = me["pos"]
        muzzle = self.mech.get("muzzle_height", 7) * (1 + me["cubes"] * self.size_per_cube)
        lower = (x, y, z + muzzle)
        solution = self.gun_solution(me, threat, lower)
        if solution is None or not self.arena.shot_blocked(lower, solution[1]):
            return None
        # Require a real building between the mechs, independently of terrain.
        distance = dist(lower[:2], solution[1][:2])
        if distance < 8 or not self.arena.building_between(lower, solution[1]):
            return None
        for rise in range(1, 13):
            if perf_counter() >= deadline:
                return None
            if self.arena.clearance(x, y, height, max(3, radius), feet=z + rise) < radius:
                return None
            elevated = x, y, z + rise + muzzle
            high = self.gun_solution(me, threat, elevated)
            if high and not self.arena.shot_blocked(elevated, high[1]):
                return z + rise
        return None

    def zone_threat(self, enemy, zone):
        """Watch enemies already inside the current circle or closing on it."""
        position = enemy["pos"][:2]
        velocity = enemy["vel"][:2]
        for center, size in ((zone["center"], zone["radius"]),
                             (zone["next_center"], zone["next_radius"])):
            distance = dist(position, center)
            if distance <= size:
                return True
            if distance > size + 35:
                continue
            inward_speed = sum((c - p) * v for c, p, v in zip(center, position, velocity)) / distance
            if inward_speed >= 1:
                return True
        return False

    def cube_target(self, obs, start, radius, height, speed, deadline):
        zone, now = obs["zone"], obs["t"]
        for cube in sorted(obs["cubes"], key=lambda item: dist(start, item["pos"][:2])):
            if perf_counter() >= deadline:
                return None
            x, y, z = cube["pos"]
            point = x, y
            distance = dist(start, point)
            if distance > 35:
                break
            if distance <= self.cube_radius or abs(self.arena.height(x, y) - z) > 3.5:
                continue
            if zone["next_radius"] - dist(point, zone["next_center"]) < radius + 8:
                continue
            travel = distance / speed
            if not self.zone_leg(zone, now, start, point, now, now + travel + 1, radius + 4):
                continue
            contested = False
            for mech in obs["mechs"]:
                if mech["kind"] != "mech":
                    continue
                mx, my = mech["pos"][:2]
                vx, vy = mech["vel"][:2]
                relative = x - mx, y - my
                eta = max(0, min(travel + 2, (relative[0] * vx + relative[1] * vy) / max(0.01, vx * vx + vy * vy)))
                if hypot(*relative) < 25 or hypot(relative[0] - vx * eta, relative[1] - vy * eta) < 18:
                    contested = True
                    break
            if not contested and self.arena.segment(start, point, radius + 1, height) is not None:
                return point
        return None

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

        next_margin = zone["next_radius"] - dist(start, zone["next_center"])
        zone_ready = me["in_zone"] and next_margin >= min(radius + 2, zone["next_radius"] * 0.25)
        enemies = [m for m in obs["mechs"] if zone_ready and m["kind"] == "mech" and self.zone_threat(m, zone)]
        threat = min(enemies, key=lambda m: dist(m["pos"], me["pos"]), default=None)
        if threat is not None and dist(threat["pos"], me["pos"]) <= 110:
            tx, ty, tz = threat["pos"]
            action["aim"] = [atan2(ty - y, tx - x), atan2(tz - z, hypot(tx - x, ty - y))]
        else:
            threat = None

        muzzle_height = self.mech.get("muzzle_height", 7) * scale
        active = self.peek
        if active is not None:
            wanted_height, started, enemy_id = active
            if me["grounded"] and now > started + 0.15:
                self.peek = None
            elif not zone_ready or now - started > 2.8 or self.zone_margin(zone, now, start, now + 1.5) < radius + 5:
                self.peek = None
                if not me["grounded"]:
                    cx, cy = zone["next_center"]
                    length = max(0.01, hypot(cx - x, cy - y))
                    action["move"] = [(cx - x) / length, (cy - y) / length]
                    return action
            else:
                enemy = next((m for m in enemies if m["id"] == enemy_id), None)
                if enemy:
                    muzzle = x, y, z + muzzle_height
                    solution = self.gun_solution(me, enemy, muzzle)
                    if solution:
                        aim, target = solution
                        action["aim"] = aim
                        yaw_error = abs((aim[0] - me["aim"][0] + pi) % (2 * pi) - pi)
                        pitch_error = abs(aim[1] - me["aim"][1])
                        if not me["grounded"] and z >= wanted_height and max(yaw_error, pitch_error) < 0.06:
                            if not self.arena.shot_blocked(muzzle, target):
                                action["fire"] = [True, False]
                    if not me["grounded"] and now - started < 1.5 and me["energy"] >= 18 and z < wanted_height and me["vel"][2] >= 0:
                        window = max(0.5, me["vel"][2] * 0.15 + 0.5)
                        action["boost"] = self.arena.clearance(x, y, height, max(3, radius), feet=z + window) >= radius
                return action
        if threat is not None and perf_counter() < deadline - 0.008:
            wanted_height = self.peek_height(me, threat, zone, now, radius, height, deadline - 0.008)
            if wanted_height is not None:
                self.peek = wanted_height, now, threat["id"]
                self.last_hop = now
                self.route.clear()
                self.last_plan = -float("inf")
                action["jump"] = True
                return action

        if self.hop is not None:
            direction, started = self.hop
            if not me["grounded"]:
                action["move"] = list(direction)
                action["boost"] = now - started < 0.30 and me["energy"] >= 20
                if self.zone_margin(zone, now, start, now + 1) < radius + 3:
                    cx, cy = zone["next_center"]
                    length = max(0.01, hypot(cx - x, cy - y))
                    action["move"] = [(cx - x) / length, (cy - y) / length]
                    action["boost"] = False
                return action
            self.hop = None
            self.route.clear()
            self.last_plan = -float("inf")

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

        speed = max(1, me["stats"].get("walk_speed", 10) * 0.75)
        if self.route and not self.zone_leg(zone, now, start, self.route[0], now, now + dist(start, self.route[0]) / speed, radius):
            self.route.clear()
            changed = True
        cube = self.cube_target(obs, start, radius, height, speed, deadline - 0.012) if zone_ready else None
        if cube != self.cube_goal:
            self.route.clear()
            changed = True
            self.cube_goal = cube
        if changed or now - self.last_plan >= (0.5 if not self.route else 2):
            target = cube if cube is not None else self.destination(start, zone, radius, height, threat, deadline - 0.008)
            outside = dist(start, zone["next_center"]) > zone["next_radius"]
            urgent = outside and (not me["in_zone"] or
                                  now + dist(start, target) / max(1, me["stats"].get("walk_speed", 10)) + 3 >= zone["shrink_end"])
            armed = all(w is not None for w in me["weapons"]) and me["hp"] >= 0.6 * me["max_hp"] and me["energy"] >= 35
            narrow_cost = 0.6 if urgent else 1.8 if armed else 4.0
            if cube is not None:
                self.route = [cube]
            elif dist(start, target) < 2:
                self.route.clear()
            else:
                route = self.plan(start, target, radius, height, narrow_cost, deadline, zone, now, speed)
                if route:
                    self.route = route
                    while self.route and dist(start, self.route[0]) < 1.8:
                        self.route.pop(0)
            self.last_plan = now

        under_fire = any(event["type"] == "damaged" and event.get("weapon") != "zone" for event in obs["events"])
        if me["grounded"] and me["energy"] >= 40 and now - self.last_hop >= 3 and (under_fire or (not self.route and self.avoid is not None)):
            if self.route:
                target = self.route[-1]
            elif self.zone_margin(zone, now, start, now + 2) < radius + 12 or threat is None:
                target = tuple(zone["next_center"])
            else:
                tx, ty, _ = threat["pos"]
                target = (2 * x - tx, 2 * y - ty)
            direction = self.hop_route(me, zone, now, target, radius, height, deadline)
            if direction is not None:
                self.hop, self.last_hop = (direction, now), now
                action.update(move=list(direction), jump=True)
                return action

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
                action["boost"] = (not me["in_zone"] or self.zone_margin(zone, now, start, now + 2) < radius + 12) and clearance > radius + 3 and length > 4
        return action


if __name__ == "__main__":
    mf.run(Bot)

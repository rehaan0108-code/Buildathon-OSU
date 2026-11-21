"""Example bot: walk to the zone, grab a weapon, shoot the nearest mech, draw what it's thinking."""
import math
from time import perf_counter
import mechfight as mf
from navigation import Navigation


class Bot:
    name = "starter-129-cube-pickup"

    def __init__(self, hello: mf.Hello):
        self.navigation = Navigation(hello)
        self.mech = hello["config"]["mech"]
        self.weapon_config = hello["config"]["weapons"]
        self.enemy_tracks = {}
        self.loot_site = None
        self.cube_radius = hello["config"]["cubes"]["pickup_radius"]
        self.reach = hello["config"]["loot"]["pickup_radius"]

    def attack_solution(self, me, enemies):
        x, y, z = me["pos"]
        muzzle = me["stats"].get("muzzle_height", self.mech.get("muzzle_height", 7))
        scale = 1 + me["cubes"] * self.navigation.growth
        solutions = []
        for slot, weapon in enumerate(me["weapons"]):
            if not weapon or weapon["kind"] not in ("gun", "laser", "mortar"):
                continue
            kind = weapon["kind"]
            stats = self.weapon_config.get(kind, {}) | weapon["stats"]
            cooldown = max(0, weapon.get("cooldown", 0))
            ready_in = max(cooldown, weapon.get("reload", 0))
            charge = weapon.get("charge", 0)
            release_at = max(stats.get("min_charge", 0.15), stats.get("charge", 0.75) - 0.10)
            if kind == "gun" and weapon.get("ammo", 1) <= 0:
                continue
            origin = x, y, z + (9.5 * scale if kind == "mortar" else muzzle)
            for enemy in enemies[:5]:
                target = (*enemy["pos"][:2], enemy["pos"][2] + 4.5 * (
                    1 + enemy.get("cubes", 0) * self.navigation.growth))
                velocity = enemy["vel"]
                # Allow aim to turn before the next shot comes off cooldown.
                yaw = math.atan2(target[1] - y, target[0] - x)
                yaw_error = abs((yaw - me["aim"][0] + math.pi) % math.tau - math.pi)
                delay = max(ready_in, yaw_error / max(0.1, me["stats"].get("turn_rate", 4)))
                if kind == "laser":
                    delay = max(delay, ready_in + max(0, release_at - charge))
                future = tuple(p + v * delay for p, v in zip(target, velocity))
                speed = stats.get("speed", 0)
                reach = stats.get("range", 0)
                if kind == "mortar":
                    # Conservative separation, including room for splash and movement.
                    minimum = max(28, 2 * stats.get("splash_radius", 7))
                    if math.dist(origin[:2], target[:2]) < minimum or math.dist(origin[:2], future[:2]) < minimum:
                        continue
                    gravity = stats.get("gravity", 22)
                    travel = self.mortar_time(origin, future, velocity, speed, gravity)
                    if travel is None:
                        continue
                elif kind == "gun" and speed > 0:
                    travel = math.dist(origin, future) / speed
                    for _ in range(3):
                        predicted = tuple(p + v * travel for p, v in zip(future, velocity))
                        travel = math.dist(origin, predicted) / speed
                elif kind == "laser":
                    travel = 0
                else:
                    continue
                impact = tuple(p + v * travel for p, v in zip(future, velocity))
                dx, dy, dz = (b - a for a, b in zip(origin, impact))
                if kind == "mortar":
                    if math.hypot(dx, dy) < minimum:
                        continue
                    dz += 0.5 * gravity * travel * travel
                elif math.dist(origin, impact) > reach:
                    continue
                pitch = math.atan2(dz, math.hypot(dx, dy))
                if self.mech["min_pitch"] <= pitch <= self.mech["max_pitch"]:
                    aim = [math.atan2(dy, dx), pitch]
                    preference = {"laser": -0.75 if charge else 0.05, "mortar": 0.10, "gun": 0.20}[kind]
                    charge_wait = max(0, release_at - charge) if kind == "laser" else 0
                    score = delay - charge_wait + preference + 0.08 * travel
                    tolerance = 0.10 if kind == "laser" else 0.06
                    aligned = abs((aim[0] - me["aim"][0] + math.pi) % math.tau - math.pi) <= tolerance
                    aligned = aligned and abs(aim[1] - me["aim"][1]) <= tolerance
                    if kind == "laser":
                        trigger = charge < release_at or not aligned
                        available = ready_in <= 0.05 and (charge > 0 or aligned)
                    else:
                        available = ready_in <= 0.05 and (aligned or kind == "gun")
                        trigger = available
                    solutions.append((score, slot, aim, impact, trigger, available, kind == "laser" and charge > 0))
        return min(solutions, key=lambda solution: (not solution[6], not solution[5], solution[0]), default=None)

    @staticmethod
    def mortar_time(origin, target, velocity, speed, gravity):
        if speed <= 0:
            return None
        def residual(t):
            displacement = [p - o + v * t for p, o, v in zip(target, origin, velocity)]
            displacement[2] += 0.5 * gravity * t * t
            return math.hypot(*displacement) - speed * t

        previous = 0.0
        for step in range(1, 37):
            t = step * 0.15
            if residual(t) <= 0:
                for _ in range(8):
                    midpoint = (previous + t) / 2
                    if residual(midpoint) > 0:
                        previous = midpoint
                    else:
                        t = midpoint
                return t
            previous = t
        return None

    def post_kill_move(self, obs, enemies):
        now = obs["t"]
        for enemy in enemies:
            self.enemy_tracks[enemy["id"]] = (enemy["pos"], now)
        defeated = {event["target"] for event in obs["events"] if event["type"] == "kill"}
        for enemy_id in defeated:
            track = self.enemy_tracks.pop(enemy_id, None)
            if track is not None and now - track[1] <= 5:
                self.loot_site = track[0], now + 8
        if self.loot_site is None:
            return None
        site, expires = self.loot_site
        if now > expires:
            self.loot_site = None
            return None

        me, zone = obs["self"], obs["zone"]
        if not me["grounded"] or not me["in_zone"]:
            return None
        start = tuple(me["pos"][:2])
        scale = 1 + me["cubes"] * self.navigation.growth
        radius = self.mech.get("radius", 2.5) * scale + 0.35
        height = self.mech.get("height", 9) * scale + 0.15
        speed = max(1, me["stats"].get("walk_speed", 11.7) * 0.75)
        if self.navigation.zone_margin(zone, now, start, now + 2) < radius + 6:
            return None

        cubes = sorted(obs["cubes"], key=lambda cube: math.dist(cube["pos"][:2], start))
        deadline = perf_counter() + 0.004
        arena = self.navigation.arena
        for cube in cubes:
            if perf_counter() >= deadline:
                break
            point = tuple(cube["pos"][:2])
            distance = math.dist(start, point)
            if distance > 35:
                break
            if distance <= self.cube_radius - 0.5 or math.dist(site, cube["pos"]) > 14:
                continue
            if abs(arena.height(*point) - cube["pos"][2]) > 4:
                continue
            travel = distance / speed
            if self.navigation.zone_margin(zone, now, point, now + travel + 2) < radius + 6:
                continue
            if not self.navigation.zone_leg(zone, now, start, point, now, now + travel, radius + 4):
                continue
            blocked = False
            for enemy in enemies:
                if enemy["id"] in defeated:
                    continue
                ex, ey = enemy["pos"][:2]
                vx, vy = enemy["vel"][:2]
                dx, dy = point[0] - ex, point[1] - ey
                eta = max(0, min(travel + 1, (dx * vx + dy * vy) / max(0.01, vx * vx + vy * vy)))
                if math.hypot(dx, dy) < 24 or math.hypot(dx - eta * vx, dy - eta * vy) < 18:
                    blocked = True
                    break
            if not blocked and arena.segment(start, point, radius, height, escape=True) is not None:
                return [(b - a) / max(4, distance) for a, b in zip(start, point)]
        return None

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        goal = obs["zone"]["next_center"]
        act: mf.Action = {"tick": obs["tick"]}
        if obs["offer"]:
            act["pick"] = 0
        near = [p for p in obs["pickups"] if math.dist(p["pos"][:2], (x, y)) < self.reach]
        if me["weapons"][1] is None and near and near[0]["kind"] != "gun":
            act["take"] = near[0]["id"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"), key=lambda m: math.dist(m["pos"], me["pos"]))
        yaw = math.atan2(goal[1] - y, goal[0] - x)
        act.update(move=[math.cos(yaw), math.sin(yaw)], aim=[yaw, 0.0], boost=not me["in_zone"], jump=obs["tick"] % 40 == 0)
        if enemies:
            t = enemies[0]["pos"]
            aim = [math.atan2(t[1] - y, t[0] - x), math.atan2(t[2] - z - 2.5, math.dist(t[:2], (x, y)))]
            fire = [bool(w and w["kind"] in ("gun", "lightning")) for w in me["weapons"]]
            target = [t[0], t[1], t[2] + 4.5]
            solution = self.attack_solution(me, enemies)
            if solution:
                _, slot, aim, target, trigger, _, _ = solution
                fire = [i == slot and trigger for i in range(2)]
                if me["weapons"][slot]["kind"] == "gun":
                    fire = [shoot or bool(w and w["kind"] == "lightning") for shoot, w in zip(fire, me["weapons"])]
            act.update(aim=aim, fire=fire)
            mf.draw.line([x, y, z + 7], list(target), "#ff5040")
        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(f"hp {me['hp']:.0f} Â· {len(enemies)} in sight")
        move = self.post_kill_move(obs, enemies)
        if move is None:
            move = self.navigation.movement(obs)
        if move is not None:
            act["move"] = move
        return self.navigation.defend_movement(obs, act, perf_counter() + 0.006)


mf.run(Bot)

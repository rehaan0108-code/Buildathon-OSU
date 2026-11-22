"""Example bot: walk to the zone, grab a weapon, shoot the nearest mech, draw what it's thinking."""
import math
from time import perf_counter
import mechfight as mf
from navigation import Navigation


class Bot:
    name = "starter-173-supply-scan"

    def __init__(self, hello: mf.Hello):
        self.navigation = Navigation(hello)
        self.mech = hello["config"]["mech"]
        self.weapon_config = hello["config"]["weapons"]
        self.enemy_tracks = {}
        self.loot_site = None
        self.rear_target = None
        self.rear_until = 0.0
        self.rear_retry = 0.0
        self.last_rear_target = None
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

    def rear_approach(self, obs, enemies):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        active = self.rear_target
        self.rear_target = None
        if active is not None:
            self.rear_retry = now + 0.7
        if (not me["grounded"] or not me["in_zone"] or me["hp"] < 0.50 * me["max_hp"]
                or obs["projectiles"] or any(event["type"] == "damaged" for event in obs["events"])):
            return None, None
        if active is None and now < self.rear_retry or active is not None and now >= self.rear_until:
            return None, None
        start = tuple(me["pos"][:2])
        scale = 1 + me["cubes"] * self.navigation.growth
        radius = self.mech.get("radius", 2.5) * scale + 0.35
        height = self.mech.get("height", 9) * scale
        speed = max(1, me["stats"].get("walk_speed", 11.7) * 0.70)
        inner_radius = max(0, zone["next_radius"] - radius - 5)
        remaining = max(0, math.dist(start, zone["next_center"]) - inner_radius)
        travel_time = remaining / speed
        shrink_end = zone["shrink_end"]
        if ((remaining > 0 and now + travel_time + 2.5 >= shrink_end)
                or self.navigation.zone_margin(zone, now, start, now + 2) < radius + 3):
            return None, None
        arena = self.navigation.arena
        deadline = perf_counter() + 0.005
        def isolation_priority(target):
            nearest_ally = min((math.dist(target["pos"][:2], other["pos"][:2])
                                for other in enemies if other["id"] != target["id"]), default=110)
            repeat = 28 if target["id"] == self.last_rear_target else 0
            return min(110, nearest_ally) - 0.25 * math.dist(start, target["pos"][:2]) - repeat

        candidates = [target for target in enemies if 13 <= math.dist(start, target["pos"][:2]) <= 90]
        if active is not None:
            candidates = [target for target in candidates if target["id"] == active]
        else:
            candidates.sort(key=isolation_priority, reverse=True)
        for target in candidates[:5]:
            if active is not None and target["id"] != active:
                continue
            enemy_position = target["pos"][:2]
            distance = math.dist(start, enemy_position)
            vx, vy = target["vel"][:2]
            yaw = target["aim"][0]
            facing = math.cos(yaw), math.sin(yaw)
            for offset in (0, 0.4, -0.4):
                if perf_counter() >= deadline:
                    return None, None
                dx = enemy_position[0] + vx * 0.5 - start[0]
                dy = enemy_position[1] + vy * 0.5 - start[1]
                heading = math.atan2(dy, dx) + offset
                move = math.cos(heading), math.sin(heading)
                point = tuple(a + 6 * v for a, v in zip(start, move))
                if any(sum((p - q) * f for p, q, f in zip(sample, enemy_position, facing))
                       > -0.40 * math.dist(sample, enemy_position) for sample in (start, point)):
                    continue
                if not self.navigation.zone_leg(zone, now, start, point, now, now + 1, radius + 8):
                    continue
                if self.navigation.zone_margin(zone, now, point, now + 2) < radius + 4:
                    continue
                if arena.segment(start, point, radius, height, escape=True) is None:
                    continue
                unsafe = False
                for other in enemies:
                    if other["id"] == target["id"]:
                        continue
                    ex, ey, ez = other["pos"]
                    evx, evy = other["vel"][:2]
                    if min(math.dist(start, (ex, ey)), math.dist(point, (ex + evx, ey + evy))) > 280:
                        continue
                    for t in (0.0, 0.5, 1.0):
                        if perf_counter() >= deadline:
                            return None, None
                        px, py = (a + t * (b - a) for a, b in zip(start, point))
                        source = ex + evx * t, ey + evy * t, ez + 7 * (
                            1 + other.get("cubes", 0) * self.navigation.growth)
                        z = arena.height(px, py)
                        if not all(arena.shot_blocked(source, (px, py, z + height * level), spacing=2)
                                   for level in (0.5, 0.85)):
                            unsafe = True
                            break
                    if unsafe:
                        break
                if unsafe:
                    continue
                self.rear_target = target["id"]
                if active is None:
                    self.rear_until = now + 3.5
                    self.last_rear_target = target["id"]
                return target, list(move)
        return None, None

    def supply_move(self, obs, enemies):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        if not me["grounded"] or not me["in_zone"]:
            return None
        start = tuple(me["pos"][:2])
        scale = 1 + me["cubes"] * self.navigation.growth
        radius = self.mech.get("radius", 2.5) * scale + 0.35
        height = self.mech.get("height", 9) * scale
        speed = max(1, me["stats"].get("walk_speed", 11.7) * 0.75)
        arena = self.navigation.arena
        deadline = perf_counter() + 0.004
        for drop in sorted(obs["drops"], key=lambda drop: math.dist(start, drop["pos"][:2])):
            if perf_counter() >= deadline:
                break
            point = tuple(drop["pos"][:2])
            distance = math.dist(start, point)
            if distance > 55:
                break
            ours = drop["holder"] == me["id"]
            if drop["holder"] is not None and not ours:
                continue
            travel = distance / speed
            if drop["lands_in"] > travel + 1:
                continue
            capture = max(0, drop["capture_s"] - (drop["progress"] if ours else 0))
            finish = now + max(travel, drop["lands_in"]) + capture + 1.5
            if self.navigation.zone_margin(zone, now, point, finish) < radius + 5:
                continue
            if not self.navigation.zone_leg(zone, now, start, point, now, now + travel, radius + 3):
                continue
            contested = False
            for enemy in enemies:
                ex, ey = enemy["pos"][:2]
                vx, vy = enemy["vel"][:2]
                dx, dy = point[0] - ex, point[1] - ey
                eta = max(0, min(finish - now, (dx * vx + dy * vy) / max(0.01, vx * vx + vy * vy)))
                if math.hypot(dx, dy) < drop["radius"] + 14 or math.hypot(dx - vx * eta, dy - vy * eta) < drop["radius"] + 8:
                    contested = True
                    break
            if contested:
                continue
            if abs(arena.height(*point) - drop["pos"][2]) > 4:
                continue
            if distance <= max(1, drop["radius"] - 2):
                return [0, 0]
            if arena.segment(start, point, radius, height, escape=True) is not None:
                return [(b - a) / max(4, distance) for a, b in zip(start, point)]
        return None

    @staticmethod
    def capture_aim(obs):
        nearby = (sound for sound in obs["sounds"]
                  if sound["kind"] in ("step", "jet", "boost")
                  and sound["dist"] in ("near", "mid") and sound["age"] <= 0.4)
        cue = min(nearby, key=lambda sound: (sound["dist"] != "near", sound["age"]), default=None)
        yaw = cue["bearing"] if cue is not None else obs["self"]["aim"][0] + 0.9
        return [(yaw + math.pi) % math.tau - math.pi, 0.0]

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
        loot_move = self.post_kill_move(obs, enemies)
        supply = self.supply_move(obs, enemies)
        target_enemy, approach_move = self.rear_approach(obs, enemies) if loot_move is None and supply is None else (None, None)
        if enemies:
            t = (target_enemy or enemies[0])["pos"]
            aim = [math.atan2(t[1] - y, t[0] - x), math.atan2(t[2] - z - 2.5, math.dist(t[:2], (x, y)))]
            fire = [bool(w and w["kind"] in ("gun", "lightning")) for w in me["weapons"]]
            target = [t[0], t[1], t[2] + 4.5]
            solution = self.attack_solution(me, [target_enemy] if target_enemy else enemies)
            if solution:
                _, slot, aim, target, trigger, _, _ = solution
                fire = [i == slot and trigger for i in range(2)]
                if me["weapons"][slot]["kind"] == "gun":
                    fire = [shoot or bool(w and w["kind"] == "lightning") for shoot, w in zip(fire, me["weapons"])]
            act.update(aim=aim, fire=fire)
            mf.draw.line([x, y, z + 7], list(target), "#ff5040")
        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(f"hp {me['hp']:.0f} Â· {len(enemies)} in sight")
        if supply is not None:
            if supply == [0, 0] and not enemies:
                act["aim"] = self.capture_aim(obs)
            act["jump"] = False
            act["boost"] = False
        move = supply if supply is not None else (loot_move if loot_move is not None else approach_move)
        if move is None:
            move = self.navigation.movement(obs)
        if move is not None:
            act["move"] = move
        return self.navigation.defend_movement(obs, act, perf_counter() + 0.006)


mf.run(Bot)

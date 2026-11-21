"""Example bot: walk to the zone, grab a weapon, shoot the nearest mech, draw what it's thinking."""
import math
from time import perf_counter
import mechfight as mf
from navigation import Navigation


class Bot:
    name = "starter-adaptive-retreat"

    def __init__(self, hello: mf.Hello):
        self.navigation = Navigation(hello)
        self.mech = hello["config"]["mech"]
        self.weapon_config = hello["config"]["weapons"]
        self.reach = hello["config"]["loot"]["pickup_radius"]

    def attack_solution(self, me, enemies):
        x, y, z = me["pos"]
        muzzle = me["stats"].get("muzzle_height", self.mech.get("muzzle_height", 7))
        scale = 1 + me["cubes"] * self.navigation.growth
        solutions = []
        for slot, weapon in enumerate(me["weapons"]):
            if not weapon or weapon["kind"] not in ("gun", "lightning", "mortar"):
                continue
            kind = weapon["kind"]
            stats = self.weapon_config.get(kind, {}) | weapon["stats"]
            cooldown = max(0, weapon.get("cooldown", 0))
            ready_in = max(cooldown, weapon.get("reload", 0))
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
                elif kind == "lightning":
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
                    preference = {"lightning": 0.0, "mortar": 0.10, "gun": 0.20}[kind]
                    score = delay + preference + 0.08 * travel
                    tolerance = math.radians(stats.get("cone_deg", 30)) * 0.4 if kind == "lightning" else 0.06
                    aligned = abs((aim[0] - me["aim"][0] + math.pi) % math.tau - math.pi) <= tolerance
                    aligned = aligned and (kind == "lightning" or abs(aim[1] - me["aim"][1]) <= tolerance)
                    can_fire = ready_in <= 0.05 and (aligned or kind == "gun")
                    solutions.append((score, slot, aim, impact, can_fire))
        return min(solutions, key=lambda solution: solution[0], default=None)

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

    @staticmethod
    def retreat_threshold(me, enemies):
        # Compare durable stats so taking damage does not itself change the threshold.
        own_loadout = {weapon["kind"] for weapon in me["weapons"] if weapon}
        for enemy in enemies:
            if any(enemy.get(field) is None for field in ("max_hp", "cubes", "weapons")):
                return 0.5
            enemy_loadout = set(enemy["weapons"])
            own_stats = (me["max_hp"], me["cubes"], len(own_loadout))
            enemy_stats = (enemy["max_hp"], enemy["cubes"], len(enemy_loadout))
            comparable = enemy_loadout <= own_loadout
            weaker = comparable and all(ours >= theirs for ours, theirs in zip(own_stats, enemy_stats)) and own_stats != enemy_stats
            if not weaker:
                return 0.5
        return 0.25 if enemies else 0.5

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
        retreating = bool(enemies) and me["hp"] < self.retreat_threshold(me, enemies) * me["max_hp"]
        if enemies and not retreating:
            t = enemies[0]["pos"]
            aim = [math.atan2(t[1] - y, t[0] - x), math.atan2(t[2] - z - 2.5, math.dist(t[:2], (x, y)))]
            solution = self.attack_solution(me, enemies)
            predicted = [t[0], t[1], t[2] + 4.5]
            if solution:
                _, slot, aim, predicted, can_fire = solution
                fire = [index == slot and can_fire for index in range(2)]
            else:
                fire = [bool(weapon and weapon["kind"] == "gun") for weapon in me["weapons"]]
            act.update(aim=aim, fire=fire)
            mf.draw.line([x, y, z + 7], list(predicted), "#ff5040")
        mf.draw.point([goal[0], goal[1], z], "#40c0ff", 6)
        mf.draw.label(f"hp {me['hp']:.0f} Â· {len(enemies)} in sight")
        move = self.navigation.retreat(obs, enemies[:2]) if retreating else None
        if move is None:
            move = self.navigation.movement(obs)
        if move is not None:
            act["move"] = move
        return self.navigation.defend_movement(obs, act, perf_counter() + 0.006)


mf.run(Bot)

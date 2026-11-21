"""Example bot: walk to the zone, grab a weapon, shoot the nearest mech, draw what it's thinking."""
import math
from time import perf_counter
import mechfight as mf
from navigation import Navigation


class Bot:
    name = "starter-lead-retreat"

    def __init__(self, hello: mf.Hello):
        self.navigation = Navigation(hello)
        self.mech = hello["config"]["mech"]
        self.reach = hello["config"]["loot"]["pickup_radius"]

    def attack_solution(self, me, enemies):
        gun = next((weapon for weapon in me["weapons"] if weapon and weapon["kind"] == "gun"), None)
        if gun is None:
            return None
        stats = gun["stats"]
        speed = stats.get("speed", 0)
        if speed <= 0:
            return None
        x, y, z = me["pos"]
        origin = x, y, z + me["stats"].get("muzzle_height", self.mech.get("muzzle_height", 7))
        solutions = []
        for enemy in enemies:
            ex, ey, ez = enemy["pos"]
            vx, vy, vz = enemy["vel"]
            midpoint = ez + 4.5 * (1 + enemy.get("cubes", 0) * self.navigation.growth)
            travel = math.dist(origin, (ex, ey, midpoint)) / speed
            for _ in range(3):
                future = ex + vx * travel, ey + vy * travel, midpoint + vz * travel
                travel = math.dist(origin, future) / speed
            dx, dy, dz = (b - a for a, b in zip(origin, future))
            pitch = math.atan2(dz, math.hypot(dx, dy))
            if travel * speed <= stats["range"] and self.mech["min_pitch"] <= pitch <= self.mech["max_pitch"]:
                solutions.append((travel, [math.atan2(dy, dx), pitch], future))
        return min(solutions, key=lambda solution: solution[0], default=None)

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
        retreating = me["hp"] < 0.5 * me["max_hp"] and bool(enemies)
        if enemies and not retreating:
            t = enemies[0]["pos"]
            aim = [math.atan2(t[1] - y, t[0] - x), math.atan2(t[2] - z - 2.5, math.dist(t[:2], (x, y)))]
            solution = self.attack_solution(me, enemies)
            predicted = [t[0], t[1], t[2] + 4.5]
            if solution:
                _, aim, predicted = solution
            act.update(aim=aim, fire=[True, True])
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

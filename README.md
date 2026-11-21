# Mech Royale base bot

Copy the files in this folder to the root of the submission repository. The bot
uses the official Python wrapper. The scorer mounts the arena separately.

## Test with the official kit

From this folder in PowerShell, set the kit path and run the replay checker.

```powershell
$kit = 'C:/Users/rehaa/Downloads/mechfight-kit/mechfight-kit'
python "$kit/check/check.py" -- python -B -u bot.py
docker build -t mech-bot .
docker run --rm -i -v "${kit}:/kit:ro" --entrypoint python mech-bot /kit/check/check.py -- python -B -u /bot/bot.py
```

The last command is a container smoke test. To check under the match limits,
run the checker from a Linux shell (or WSL with Docker integration) against a restricted bot container. Set the kit path to its Linux-visible location. The checker normalizes the map path using host path rules, so use Linux for this step.

```bash
kit='/mnt/c/Users/rehaa/Downloads/mechfight-kit/mechfight-kit'
python3 "$kit/check/check.py" --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 --mount "type=bind,source=$kit/arena,target=/arena,readonly" mech-bot
```

Submit the public GitHub repository URL on the Mech Royale track page for
practice matches. The replay checker verifies messages and response timing.

## Base behavior

The bot uses a ground grid, sampled clearance, and time-bounded A* to route
toward the next circle. Narrow stretches cost more, particularly without a
second weapon, at least 60% HP, and 35 energy. These are initial tuning values.
Movement monitors progress and avoids recently stalled waypoints.

Cover selection samples straight lines from a nearby threat to several heights
on our mech. This is approximate. It can miss thin geometry and does not predict
individual incoming shots, mortar arcs or splash. The bot is a survival baseline
with no firing policy, rooftop navigation or flanking.

#!/usr/bin/env python3
"""
Play full STS2 runs using the headless simulator.

Usage:
  python3 play_full_run.py <num_runs> [character] [--policy random|heuristic]

Arguments:
  num_runs    Positive integer: number of runs to play
  character   One of: Ironclad, Silent, Defect, Regent, Necrobinder (default: Ironclad)

Examples:
  python3 play_full_run.py 5
  python3 play_full_run.py 3 Silent
"""

import argparse
import json
import subprocess
import sys
import random
import os
from game_log import GameLogger

VALID_CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]
VALID_POLICIES = ["random", "heuristic"]

def _find_dotnet():
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"), "dotnet"]:
        try:
            if subprocess.run([p, "--version"], capture_output=True, timeout=5).returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "dotnet"

DOTNET = _find_dotnet()
PROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src", "Sts2Headless", "Sts2Headless.csproj")


def _name(value) -> str:
    if isinstance(value, dict):
        return value.get("en") or value.get("zh") or next(iter(value.values()), "")
    return str(value or "")


def _stats(card: dict) -> dict:
    return card.get("stats") or {}


def _incoming_damage(enemies: list[dict]) -> int:
    total = 0
    for enemy in enemies:
        for intent in enemy.get("intents") or []:
            if intent.get("type") in ("Attack", "DeathBlow"):
                damage = max(0, int(intent.get("damage") or 0))
                hits = max(1, int(intent.get("hits") or 1))
                total += damage * hits
    return total


def _enemy_threat(enemy: dict) -> int:
    incoming = _incoming_damage([enemy])
    hp = max(0, int(enemy.get("hp") or 0))
    block = max(0, int(enemy.get("block") or 0))
    return incoming * 100 - hp - block


def _pick_target(enemies: list[dict], card_damage: int = 0) -> int | None:
    alive = [enemy for enemy in enemies if enemy.get("hp", 0) > 0]
    if not alive:
        return None

    def target_index(enemy: dict) -> int:
        return int(enemy.get("index", enemies.index(enemy)))

    if card_damage > 0:
        killable = [
            enemy for enemy in alive
            if int(enemy.get("hp") or 0) + int(enemy.get("block") or 0) <= card_damage
        ]
        if killable:
            return target_index(max(killable, key=_enemy_threat))

    return target_index(max(alive, key=lambda e: (_enemy_threat(e), -int(e.get("hp") or 0))))


def _is_bad_card(card: dict) -> bool:
    name = _name(card.get("name"))
    keywords = card.get("keywords") or []
    return (
        card.get("type") in ("Status", "Curse")
        or "Unplayable" in keywords
        or name in {"Slimed", "Burn", "Wound", "Dazed", "Infection"}
    )


def _choose_heuristic_card(state: dict) -> tuple[dict, int | None] | None:
    hand = state.get("hand", [])
    enemies = state.get("enemies", [])
    energy = int(state.get("energy") or 0)
    player = state.get("player", {})
    hp = int(player.get("hp") or 0)
    block = int(player.get("block") or 0)
    incoming = _incoming_damage(enemies)
    block_gap = max(0, incoming - block)
    low_hp = hp > 0 and hp <= max(18, incoming + 4)

    best = None
    best_score = -10_000
    for card in hand:
        cost = int(card.get("cost") or 0)
        if not card.get("can_play", False) or cost > energy or _is_bad_card(card):
            continue

        ctype = card.get("type", "")
        target_type = card.get("target_type", "")
        stats = _stats(card)
        damage = int(stats.get("damage") or stats.get("damage_by_target") or 0)
        card_block = int(stats.get("block") or 0)

        score = 0
        if cost == 0:
            score += 25
        if ctype == "Power":
            score += 35 if incoming <= 12 and hp > 30 else -15
        if target_type == "AnyEnemy":
            score += 18 + damage * 2
            if block_gap > 0 and low_hp:
                score -= 25
        elif card_block > 0:
            if block_gap > 0:
                score += 20 + min(card_block, block_gap) * 4
                if low_hp:
                    score += 20
            else:
                score -= 25
        elif ctype == "Skill" and incoming == 0:
            score += 5
        else:
            score += 8

        score -= cost * 3

        target = None
        if target_type == "AnyEnemy":
            target = _pick_target(enemies, damage)
            if target is None:
                continue

        if score > best_score:
            best_score = score
            best = (card, target)
    return best


def _choose_map_node(state: dict, policy: str) -> dict | None:
    choices = state.get("choices", [])
    if not choices:
        return None
    if policy == "random":
        return random.choice(choices)

    player = state.get("player", {})
    hp = player.get("hp") or 1
    max_hp = player.get("max_hp") or hp or 1
    hp_pct = hp / max_hp
    gold = player.get("gold") or 0
    floor = (state.get("context") or {}).get("floor") or state.get("floor") or 1

    def score(choice: dict) -> int:
        room = choice.get("type", "")
        values = {
            "Boss": 100,
            "Treasure": 75,
            "RestSite": 70 if hp_pct < 0.7 or floor >= 14 else 35,
            "Shop": 65 if gold >= 120 else 25,
            "Unknown": 28,
            "Monster": 50 if hp_pct >= 0.45 else 15,
            "Elite": 55 if hp_pct >= 0.8 else 5,
        }
        return values.get(room, 20)

    return max(choices, key=score)


def _choose_card_reward(state: dict, policy: str) -> int | None:
    cards = state.get("cards", [])
    if not cards:
        return None
    if policy == "random":
        return 0

    player = state.get("player", {})
    deck = player.get("deck", [])
    deck_size = player.get("deck_size") or len(deck)
    attack_count = sum(1 for c in deck if c.get("type") == "Attack")
    existing = {}
    for card in deck:
        name = _name(card.get("name"))
        existing[name] = existing.get(name, 0) + 1

    rarity_score = {"Common": 0, "Uncommon": 8, "Rare": 16}
    best_index = None
    best_score = -10_000
    for card in cards:
        name = _name(card.get("name"))
        stats = _stats(card)
        ctype = card.get("type", "")
        cost = int(card.get("cost") or 0)
        damage = int(stats.get("damage") or 0)
        block = int(stats.get("block") or 0)
        score = rarity_score.get(card.get("rarity"), 0) - cost * 2

        if ctype == "Attack":
            score += 8 + damage
            if deck_size <= 13:
                score += 14
            if attack_count < 6:
                score += 12
        elif ctype == "Power":
            score += 20
        elif ctype == "Skill":
            score += 4 + block
            if not block and deck_size <= 13:
                score -= 8
        if existing.get(name, 0) >= 2:
            score -= 20
        if deck_size >= 18 and score < 30:
            score -= 30

        if score > best_score:
            best_score = score
            best_index = card.get("index", cards.index(card))

    if deck_size >= 20 and best_score < 35:
        return None
    return best_index


def _choose_rest_option(state: dict, policy: str) -> dict | None:
    enabled = [o for o in state.get("options", []) if o.get("is_enabled", True)]
    if not enabled:
        return None
    heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
    smith = next((o for o in enabled if o.get("option_id") == "SMITH"), None)
    if policy == "random":
        return heal or enabled[0]

    player = state.get("player", {})
    hp = player.get("hp") or 1
    max_hp = player.get("max_hp") or hp or 1
    if heal and hp / max_hp < 0.65:
        return heal
    return smith or heal or enabled[0]


def play_run(seed: str, character: str = "Ironclad", policy: str = "random",
             verbose: bool = True, log: bool = True):
    """Play a complete run and return the result."""
    logger = GameLogger(character, seed, enabled=log)
    proc = subprocess.Popen(
        [DOTNET, "run", "--no-build", "--project", PROJECT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if not verbose else None,
        text=True,
        bufsize=1,
    )

    def read_json_line() -> dict:
        """Read a line from stdout, skipping non-JSON lines (build warnings etc.)"""
        while True:
            resp_line = proc.stdout.readline().strip()
            if not resp_line:
                raise RuntimeError("No response from simulator (EOF)")
            if resp_line.startswith("{"):
                return json.loads(resp_line)
            # Skip non-JSON lines (build warnings, etc.)
            if verbose:
                print(f"  [skip] {resp_line[:120]}")

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        resp = read_json_line()
        logger.log_state(resp)
        if verbose:
            rtype = resp.get("type", "?")
            decision = resp.get("decision", "")
            if rtype == "decision":
                player = resp.get("player", {})
                hp = player.get("hp", "?")
                max_hp = player.get("max_hp", "?")
                gold = player.get("gold", "?")
                act = resp.get("act", "?")
                floor = resp.get("floor", "?")
                print(f"  < {rtype}/{decision} act={act} floor={floor} hp={hp}/{max_hp} gold={gold}")
            else:
                print(f"  < {json.dumps(resp)[:200]}")
        return resp

    step = 0
    try:
        # Read ready message (may need to skip build warnings)
        ready = read_json_line()
        if ready.get("type") != "ready":
            print(f"  Unexpected initial response: {ready}")
            return {"victory": False, "seed": seed, "error": "bad_init"}
        if verbose:
            print(f"Connected: {ready}")

        # Start run
        state = send({"cmd": "start_run", "character": character, "seed": seed})

        step = 0
        max_steps = 500  # Safety limit
        stuck_count = 0
        last_state_key = None

        while step < max_steps:
            step += 1

            if state.get("type") == "error":
                print(f"  ERROR: {state.get('message', 'unknown')}")
                break

            decision = state.get("decision", "")

            # Stuck detection — use comprehensive state key
            hand_len = len(state.get("hand", []))
            enemy_hp = sum(e.get("hp", 0) for e in state.get("enemies", []))
            energy = state.get("energy", 0)
            state_key = f"{decision}:{state.get('round')}:{state.get('player',{}).get('hp')}:{hand_len}:{enemy_hp}:{energy}"
            if state_key == last_state_key:
                stuck_count += 1
                if stuck_count > 20:
                    print(f"  STUCK after {step} steps, forcing quit")
                    return {"victory": False, "seed": seed, "steps": step,
                            "act": state.get("act"), "floor": state.get("floor"),
                            "hp": state.get("player", {}).get("hp"),
                            "max_hp": state.get("player", {}).get("max_hp")}
            else:
                stuck_count = 0
                last_state_key = state_key

            if decision == "game_over":
                victory = state.get("victory", False)
                player = state.get("player", {})
                print(f"\n{'VICTORY' if victory else 'DEFEAT'} at act {state.get('act')}, "
                      f"floor {state.get('floor')} "
                      f"(HP: {player.get('hp')}/{player.get('max_hp')}, "
                      f"Gold: {player.get('gold')}, "
                      f"Deck: {player.get('deck_size')} cards)")
                return {
                    "victory": victory,
                    "seed": seed,
                    "steps": step,
                    "act": state.get("act"),
                    "floor": state.get("floor"),
                    "hp": player.get("hp"),
                    "max_hp": player.get("max_hp"),
                }

            elif decision == "map_select":
                choices = state.get("choices", [])
                if not choices:
                    print("  No map choices available!")
                    break
                choice = _choose_map_node(state, policy)
                state = send({
                    "cmd": "action",
                    "action": "select_map_node",
                    "args": {"col": choice["col"], "row": choice["row"]}
                })

            elif decision == "combat_play":
                hand = state.get("hand", [])
                energy = state.get("energy", 0)
                enemies = state.get("enemies", [])

                if policy == "heuristic":
                    selected = _choose_heuristic_card(state)
                    playable = [selected[0]] if selected else []
                else:
                    # Baseline strategy: play the first playable card until out of energy.
                    playable = [c for c in hand if c.get("can_play", False)
                               and (c.get("cost", 0) <= energy)]

                if playable:
                    card = playable[0]
                    args = {"card_index": card["index"]}
                    if card.get("target_type") == "AnyEnemy" and enemies:
                        if policy == "heuristic":
                            args["target_index"] = selected[1]
                        else:
                            # Baseline target: first enemy.
                            args["target_index"] = 0
                    state = send({
                        "cmd": "action",
                        "action": "play_card",
                        "args": args
                    })
                else:
                    # End turn - retry a few times if we get "Not in play phase"
                    for retry in range(5):
                        state = send({
                            "cmd": "action",
                            "action": "end_turn"
                        })
                        if state.get("type") != "error":
                            break
                        import time
                        time.sleep(0.5)
                    if state.get("type") == "error":
                        # Try proceeding instead
                        state = send({"cmd": "action", "action": "proceed"})

            elif decision == "event_choice":
                options = state.get("options", [])
                if options:
                    # Pick first unlocked option
                    choice = next((o for o in options if not o.get("is_locked")), options[0])
                    state = send({
                        "cmd": "action",
                        "action": "choose_option",
                        "args": {"option_index": choice["index"]}
                    })
                    if state and state.get("type") == "error":
                        state = send({"cmd": "action", "action": "leave_room"})
                else:
                    state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "rest_site":
                choice = _choose_rest_option(state, policy)
                if choice:
                    state = send({
                        "cmd": "action",
                        "action": "choose_option",
                        "args": {"option_index": choice["index"]}
                    })
                    if state and state.get("type") == "error":
                        state = send({"cmd": "action", "action": "leave_room"})
                else:
                    state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "card_reward":
                card_index = _choose_card_reward(state, policy)
                if card_index is not None:
                    state = send({
                        "cmd": "action",
                        "action": "select_card_reward",
                        "args": {"card_index": card_index}
                    })
                else:
                    state = send({"cmd": "action", "action": "skip_card_reward"})

            elif decision == "bundle_select":
                state = send({"cmd": "action", "action": "select_bundle",
                             "args": {"bundle_index": 0}})

            elif decision == "card_select":
                # Auto-select first card
                cards = state.get("cards", [])
                if cards:
                    state = send({"cmd": "action", "action": "select_cards",
                                 "args": {"indices": "0"}})
                else:
                    state = send({"cmd": "action", "action": "skip_select"})

            elif decision == "shop":
                state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "unknown":
                state = send({"cmd": "action", "action": "proceed"})

            else:
                state = send({"cmd": "action", "action": "proceed"})
                state = send({"cmd": "action", "action": "proceed"})

        print(f"  Reached max steps ({max_steps})")
        return {"victory": False, "seed": seed, "steps": step, "timeout": True}

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return {"victory": False, "seed": seed, "steps": step, "error": str(e)}

    finally:
        logger.close()
        if logger.path:
            print(f"  [log] Saved to {logger.path}")
        try:
            proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            proc.stdin.flush()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(
        description="Play full STS2 runs using the headless simulator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Valid characters: " + ", ".join(VALID_CHARACTERS) + "\n"
            "Policies: random (existing baseline), heuristic (rule-based)"
        ),
    )
    parser.add_argument("num_runs", type=int, help="Number of runs to play (must be positive)")
    parser.add_argument("character", nargs="?", default="Ironclad",
                        choices=VALID_CHARACTERS, metavar="character",
                        help=f"Character to play as (default: Ironclad). Choices: {', '.join(VALID_CHARACTERS)}")
    parser.add_argument("--policy", choices=VALID_POLICIES, default="random",
                        help="Autoplay policy to use (default: random)")
    args = parser.parse_args()

    if args.num_runs <= 0:
        parser.error(f"num_runs must be a positive integer, got {args.num_runs}")

    num_runs = args.num_runs
    character = args.character
    policy = args.policy

    print(f"Playing {num_runs} runs as {character} using {policy} policy")
    print("=" * 60)

    results = []
    for i in range(num_runs):
        seed = f"run_{i+1}"
        print(f"\n--- Run {i+1}/{num_runs} (seed: {seed}) ---")
        result = play_run(seed, character, policy=policy, verbose=True)
        results.append(result)
        print()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    wins = sum(1 for r in results if r and r.get("victory"))
    completed = sum(1 for r in results if r and not r.get("timeout"))
    for i, r in enumerate(results):
        if r:
            status = "WIN" if r.get("victory") else ("TIMEOUT" if r.get("timeout") else "LOSS")
            print(f"  Run {i+1}: {status} | seed={r.get('seed')} steps={r.get('steps')} "
                  f"act={r.get('act')} floor={r.get('floor')}")
    print(f"\nWins: {wins}/{num_runs}, Completed: {completed}/{num_runs}")


if __name__ == "__main__":
    main()

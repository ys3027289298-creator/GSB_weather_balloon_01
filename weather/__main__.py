"""CMD interface and fixed-data demo for the weather balloon game.

Usage:
    python -m weather            # run the deterministic demo
    python -m weather demo       # same as above
    python -m weather <command>  # run a single command

Commands:
    ingest <sensor> <kind> <value> <unit> <sampled_at>
    calibrate <sensor> <scale> <offset>
    round
    launch
    cancel
    status
    save <path>
    load <path>
    demo
"""

import json
import sys

from .core import Game, GameError

DEMO_SCRIPT = [
    "ingest s1 temperature 20 C 0",
    "ingest s2 temperature 68 F 0",
    "ingest s2 temperature 68 F 0",
    "ingest s1 wind 12 kn 0",
    "ingest s3 wind 8 kn 0",
    "ingest s2 wind 99 kn -9",
    "round",
    "calibrate s1 1.0 0.5",
    "calibrate s2 0 1",
    "launch",
    "cancel",
    "status",
]


def execute(game, line):
    """Run one command line against the game; returns (ok, message)."""
    parts = str(line).split()
    if not parts:
        return False, "empty command"
    command, args = parts[0].lower(), parts[1:]
    try:
        if command == "ingest":
            sensor, kind, value, unit, sampled_at = args
            reading = game.ingest(sensor, kind, float(value), unit, int(sampled_at))
            return True, f"ingested seq={reading.seq}"
        if command == "calibrate":
            sid, scale, offset = args
            return game.calibrate(sid, float(scale), float(offset))
        if command == "round":
            summary = game.run_round()
            return True, (
                f"round {summary.tick}: count={summary.count} "
                f"wind={summary.wind} temperature={summary.temperature}"
            )
        if command == "launch":
            record = game.launch()
            return True, f"launched seq={record['seq']}"
        if command == "cancel":
            return game.cancel()
        if command == "status":
            return True, json.dumps(game.to_dict(), sort_keys=True)
        if command == "save":
            game.save(args[0])
            return True, f"saved to {args[0]}"
        if command == "load":
            other = Game.load(args[0])
            game.__dict__.update(other.__dict__)
            return True, f"loaded from {args[0]}"
        if command == "demo":
            return True, "\n".join(run_demo(game))
        return False, f"unknown command: {command}"
    except (GameError, ValueError, IndexError, TypeError) as exc:
        return False, f"error: {exc}"


def run_demo(game=None):
    """Run the fixed demo script; output is deterministic across runs."""
    game = game if game is not None else Game.default()
    lines = []
    for command in DEMO_SCRIPT:
        ok, message = execute(game, command)
        status = "ok" if ok else "fail"
        lines.append(f"> {command}\n{status}: {message}")
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    game = Game.default()
    if not argv or argv[0] == "demo":
        for line in run_demo(game):
            print(line)
        return 0
    ok, message = execute(game, " ".join(argv))
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate synthetic responses with a single global virtual-time schedule."""

import argparse
import csv
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path


SCENARIOS = ("success", "rate-limit", "verification")


@dataclass(frozen=True)
class Event:
    number: int
    virtual_seconds: float
    http_status: int
    outcome: str
    synthetic: bool = True


def simulate(count=10000, rate=5, scenario="success"):
    if type(count) is not int or not 1 <= count <= 1000000:
        raise ValueError("count must be an integer between 1 and 1000000")
    if type(rate) is not int or not 1 <= rate <= 1000:
        raise ValueError("rate must be an integer between 1 and 1000")
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario: " + str(scenario))

    events = []
    delay = 0.0
    for number in range(1, count + 1):
        now = round((number - 1) / rate + delay, 6)
        if number == 3 and scenario == "verification":
            events.append(Event(number, now, 403, "verification_required"))
            break
        if number == 3 and scenario == "rate-limit":
            events.append(Event(number, now, 429, "throttled"))
            # The next slot must be at least 30 virtual seconds after the 429.
            delay += 30 - 1 / rate
        else:
            events.append(Event(number, now, 200, "ok"))
    return events


def write_csv(path, events):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[item.name for item in fields(Event)])
        writer.writeheader()
        writer.writerows(asdict(event) for event in events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10000)
    parser.add_argument("--rate", type=int, default=5,
                        help="maximum events per virtual second, across the whole queue")
    parser.add_argument("--scenario", choices=SCENARIOS, default="success")
    parser.add_argument("--output", type=Path, help="optional CSV path; existing files are preserved")
    args = parser.parse_args(argv)
    try:
        events = simulate(args.count, args.rate, args.scenario)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        try:
            write_csv(args.output, events)
        except OSError as exc:
            parser.exit(1, "Report write failed: {}\n".format(exc))
    stopped = events[-1].outcome == "verification_required"
    summary = {
        "mode": "offline_synthetic",
        "scenario": args.scenario,
        "requested_events": args.count,
        "completed_events": len(events),
        "ok": sum(event.outcome == "ok" for event in events),
        "throttled": sum(event.outcome == "throttled" for event in events),
        "verification_required": sum(event.outcome == "verification_required" for event in events),
        "virtual_seconds": events[-1].virtual_seconds,
        "stopped_early": stopped,
    }
    print(json.dumps(summary, indent=2))
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the agent scenario suite on ElevenLabs simulation tests (docs/OBSERVABILITY_AND_EVALS.md, 2.3-2.5).

    uv run python scripts/run_evals.py --repeat 3

Scenarios live in evals/scenarios.json. Tools are mocked, so the suite is hermetic and needs no running API.
Each scenario is upserted as an ElevenLabs test, run `--repeat` times, and the process exits non-zero if any
run fails: hard rules must pass on every run. Results are written to evals/results/latest.json.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from recovery.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "cart-recovery/"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", help="run a single scenario by name")
    args = ap.parse_args()

    s = get_settings()
    http = httpx.Client(
        base_url="https://api.elevenlabs.io",
        headers={"xi-api-key": s.elevenlabs_api_key.get_secret_value()},
        timeout=60,
    )

    def call(method: str, path: str, **kw: Any) -> Any:
        res = http.request(method, path, **kw)
        if res.is_error:
            raise SystemExit(f"{method} {path} -> {res.status_code}: {res.text[:500]}")
        return res.json() if res.content else None

    suite = json.loads((ROOT / "evals/scenarios.json").read_text())
    defaults = suite["defaults"]
    tool_ids = {
        t["tool_config"]["name"]: t["id"]
        for t in call("GET", "/v1/convai/tools", params={"page_size": 100})["tools"]
    }
    existing = {
        t["name"]: t["id"]
        for t in call("GET", "/v1/convai/agent-testing", params={"page_size": 100})["tests"]
    }

    test_ids: dict[str, str] = {}
    for sc in suite["scenarios"]:
        if args.only and sc["name"] != args.only:
            continue
        mocks = {**defaults["mocks"], **sc.get("mocks", {})}
        body = {
            "type": "simulation",
            "name": PREFIX + sc["name"],
            "simulation_scenario": sc["persona"],
            "simulation_max_turns": sc.get("max_turns", defaults["max_turns"]),
            "dynamic_variables": {**defaults["dynamic_variables"], **sc.get("dynamic_variables", {})},
            "success_conditions": sc["expect"] + defaults["hard_rules"],
            "tool_mock_config": {"mocking_strategy": "all", "fallback_strategy": "raise_error"},
            "tool_mock_overrides": {
                tool_ids[name]: [{"mock_result": result}] for name, result in mocks.items()
            },
        }
        name = PREFIX + sc["name"]
        if name in existing:
            call("PUT", f"/v1/convai/agent-testing/{existing[name]}", json=body)
            test_ids[sc["name"]] = existing[name]
        else:
            test_ids[sc["name"]] = call("POST", "/v1/convai/agent-testing/create", json=body)["id"]

    invocation = call(
        "POST",
        f"/v1/convai/agents/{s.elevenlabs_agent_id}/run-tests",
        json={
            "tests": [{"test_id": t} for t in test_ids.values()],
            "repeat_count": args.repeat,
        },
    )
    print(f"invocation {invocation['id']}: {len(test_ids)} scenarios x {args.repeat}")

    runs: list[dict[str, Any]] = []
    for _ in range(120):
        runs = call("GET", f"/v1/convai/test-invocations/{invocation['id']}")["test_runs"]
        if all(r["status"] != "pending" for r in runs):
            break
        time.sleep(5)

    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_name.setdefault(r.get("test_name", r["test_id"]).removeprefix(PREFIX), []).append(r)
    failed = 0
    for name, rs in sorted(by_name.items()):
        passed = sum(r["status"] == "passed" for r in rs)
        failed += len(rs) - passed
        mark = "PASS" if passed == len(rs) else "FAIL"
        print(f"  {mark}  {name:28} {passed}/{len(rs)}")
        for r in rs:
            if r["status"] != "passed":
                print(f"        {json.dumps(r.get('condition_result'))[:400]}")

    out = ROOT / "evals/results"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps({"invocation": invocation["id"], "runs": runs}, indent=1))
    print(f"\n{len(runs) - failed}/{len(runs)} runs passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

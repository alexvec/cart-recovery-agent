"""Publish the ElevenLabs agent from the repo (configuration as code; never edit it in the dashboard).

    uv run python scripts/sync_agent.py --base-url https://<public-host>

Idempotent. Ensures, in order:
1. the tool secret is stored in ElevenLabs' secret store (tools reference it, never the raw value);
2. a post-call webhook pointing at <base-url>/webhooks/elevenlabs (its signing secret is written to .env);
3. the server tools from agent/tools.json, pointing at <base-url>;
4. the agent from agent/agent.json + agent/prompt.md (its id is written to .env).
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

import httpx

from recovery.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.elevenlabs.io"
SECRET_NAME = "cart-recovery-tool-secret"  # noqa: S105 - a name, not a secret
WEBHOOK_NAME = "cart-recovery-post-call"


def set_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    text = env.read_text()
    line = f"{key}={value}"
    text = (
        re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        if re.search(rf"^{key}=", text, re.M)
        else f"{text}\n{line}\n"
    )
    env.write_text(text)


class ElevenLabs:
    def __init__(self, api_key: str) -> None:
        self.http = httpx.Client(base_url=API, headers={"xi-api-key": api_key}, timeout=30)

    def call(self, method: str, path: str, **kw: Any) -> Any:
        res = self.http.request(method, path, **kw)
        if res.is_error:
            raise SystemExit(f"{method} {path} -> {res.status_code}: {res.text[:500]}")
        return res.json() if res.content else None


def ensure_secret(el: ElevenLabs, value: str) -> str:
    for s in el.call("GET", "/v1/convai/secrets")["secrets"]:
        if s["name"] == SECRET_NAME:
            return str(s["secret_id"])
    return str(
        el.call("POST", "/v1/convai/secrets", json={"type": "new", "name": SECRET_NAME, "value": value})[
            "secret_id"
        ]
    )


def ensure_webhook(el: ElevenLabs, url: str) -> str:
    """Webhook URLs can't be edited, so a changed URL means replace (and a new signing secret)."""
    for w in el.call("GET", "/v1/workspace/webhooks")["webhooks"]:
        if w.get("name") == WEBHOOK_NAME:
            if w.get("webhook_url") == url and get_settings().elevenlabs_webhook_secret.get_secret_value():
                return str(w["webhook_id"])
            el.call("DELETE", f"/v1/workspace/webhooks/{w['webhook_id']}")
    created = el.call(
        "POST",
        "/v1/workspace/webhooks",
        json={
            "settings": {"auth_type": "hmac", "name": WEBHOOK_NAME, "webhook_url": url},
        },
    )
    set_env("ELEVENLABS_WEBHOOK_SECRET", created["webhook_secret"])
    return str(created["webhook_id"])


def tool_config(spec: dict[str, Any], base_url: str, secret_id: str) -> dict[str, Any]:
    properties: dict[str, Any] = {"workflow_id": {"type": "string", "dynamic_variable": "workflow_id"}}
    properties.update(spec["body"])
    return {
        "tool_config": {
            "type": "webhook",
            "name": spec["name"],
            "description": spec["description"],
            "response_timeout_secs": 10,
            "api_schema": {
                "url": base_url + spec["path"],
                "method": "POST",
                "request_headers": {"X-Tool-Secret": {"secret_id": secret_id}},
                "request_body_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                },
            },
        }
    }


def ensure_tools(el: ElevenLabs, base_url: str, secret_id: str) -> list[str]:
    existing = {
        t["tool_config"]["name"]: t["id"]
        for t in el.call("GET", "/v1/convai/tools", params={"page_size": 100})["tools"]
    }
    ids = []
    for spec in json.loads((ROOT / "agent/tools.json").read_text()):
        body = tool_config(spec, base_url, secret_id)
        if spec["name"] in existing:
            el.call("PATCH", f"/v1/convai/tools/{existing[spec['name']]}", json=body)
            ids.append(existing[spec["name"]])
        else:
            ids.append(el.call("POST", "/v1/convai/tools", json=body)["id"])
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="public https URL of the API, e.g. the tunnel")
    base_url = ap.parse_args().base_url.rstrip("/")
    s = get_settings()
    el = ElevenLabs(s.elevenlabs_api_key.get_secret_value())

    secret_id = ensure_secret(el, s.tool_secret.get_secret_value())
    webhook_id = ensure_webhook(el, f"{base_url}/webhooks/elevenlabs")
    tool_ids = ensure_tools(el, base_url, secret_id)

    agent = json.loads((ROOT / "agent/agent.json").read_text())
    prompt = agent["conversation_config"]["agent"]["prompt"]
    prompt["prompt"] = (ROOT / "agent/prompt.md").read_text()
    prompt["tool_ids"] = tool_ids
    agent["platform_settings"]["workspace_overrides"] = {
        "webhooks": {"post_call_webhook_id": webhook_id, "events": ["transcript", "call_initiation_failure"]},
    }

    if s.elevenlabs_agent_id:
        el.call("PATCH", f"/v1/convai/agents/{s.elevenlabs_agent_id}", json=agent)
        agent_id = s.elevenlabs_agent_id
    else:
        agent_id = el.call("POST", "/v1/convai/agents/create", json=agent)["agent_id"]
        set_env("ELEVENLABS_AGENT_ID", agent_id)
    print(f"agent {agent_id} synced: {len(tool_ids)} tools, post-call webhook {webhook_id} -> {base_url}")


if __name__ == "__main__":
    main()

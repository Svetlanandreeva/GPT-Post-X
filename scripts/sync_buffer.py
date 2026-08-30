#!/usr/bin/env python3
"""Publish the newest READY social post from the repository to Buffer."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.buffer.com"
CONFIG_PATH = ROOT / "config.json"
QUEUE_PATH = ROOT / "posts" / "posts.json"
STATE_PATH = ROOT / "state" / "social_queue_state.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_created_at(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def gql(api_key: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False))
    return payload["data"]


def get_organization_id(api_key: str) -> str:
    data = gql(api_key, """query { account { organizations { id name } } }""")
    orgs = data["account"]["organizations"]
    if not orgs:
        raise RuntimeError("No Buffer organization found.")
    return orgs[0]["id"]


def get_channels(api_key: str, organization_id: str) -> list[dict[str, Any]]:
    safe_org = organization_id.replace('"', '\\"')
    data = gql(api_key, f"""query {{ channels(input: {{ organizationId: \"{safe_org}\" }}) {{ id name service }} }}""")
    return data["channels"]


def create_scheduled_post(api_key: str, channel_id: str, text: str, due_at: str) -> dict[str, Any]:
    data = gql(
        api_key,
        """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess { post { id text dueAt channelId status } }
            ... on MutationError { message }
          }
        }
        """,
        {"input": {"text": text, "channelId": channel_id, "schedulingType": "automatic", "mode": "customScheduled", "dueAt": due_at, "aiAssisted": True}},
    )
    result = data["createPost"]
    if "message" in result and "post" not in result:
        raise RuntimeError(result["message"])
    return result["post"]


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    wanted_services = set(config.get("services", ["twitter", "threads"]))
    max_queue_age_hours = int(config.get("max_queue_age_hours", 8))
    publish_delay_minutes = int(config.get("publish_delay_minutes", 5))

    posts = load_json(QUEUE_PATH, [])
    ready: list[dict[str, Any]] = []
    for post in posts:
        if str(post.get("status", "")).upper() != "READY":
            continue
        if not post.get("id") or not post.get("created_at"):
            continue
        item = dict(post)
        item["_created_at"] = parse_created_at(str(post["created_at"]))
        ready.append(item)

    if not ready:
        print("No READY posts in repository queue.")
        return 0

    post = max(ready, key=lambda item: item["_created_at"])
    post_id = str(post["id"])
    created_at: datetime = post["_created_at"]

    age = datetime.now(timezone.utc) - created_at
    if age > timedelta(hours=max_queue_age_hours):
        print(f"Newest READY post {post_id} is stale ({age.total_seconds()/3600:.1f}h).")
        return 0

    texts = {"twitter": str(post.get("x") or "").strip(), "threads": str(post.get("threads") or "").strip()}
    for service in wanted_services:
        if not texts.get(service):
            raise RuntimeError(f"{post_id}: missing text for {service}")

    # Validate all platform constraints BEFORE sending either post.
    if "twitter" in wanted_services and len(texts["twitter"]) > 280:
        raise RuntimeError(f"{post_id}: X text is over 280 characters")
    if "threads" in wanted_services and len(texts["threads"]) > 500:
        raise RuntimeError(f"{post_id}: Threads text is over 500 characters")

    state: dict[str, list[str]] = load_json(STATE_PATH, {})
    done_services = set(state.get(post_id, []))
    if wanted_services.issubset(done_services):
        print(f"{post_id}: already published to all configured services.")
        return 0

    api_key = os.getenv("BUFFER_API_KEY", "").strip()
    if not api_key:
        print("ERROR: BUFFER_API_KEY is required.", file=sys.stderr)
        return 2

    organization_id = get_organization_id(api_key)
    channels = get_channels(api_key, organization_id)
    service_channels: dict[str, dict[str, Any]] = {}
    for channel in channels:
        service = channel.get("service")
        if service in wanted_services and service not in service_channels:
            service_channels[service] = channel

    missing = wanted_services - set(service_channels)
    if missing:
        raise RuntimeError("Missing Buffer channel(s): " + ", ".join(sorted(missing)))

    failures: list[str] = []
    for service in sorted(wanted_services):
        if service in done_services:
            print(f"SKIP {post_id} -> {service}: already published")
            continue
        due_at = (datetime.now(timezone.utc) + timedelta(minutes=publish_delay_minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        try:
            result = create_scheduled_post(api_key, service_channels[service]["id"], texts[service], due_at)
            print(f"CREATED {post_id} -> {service}: {result['id']} @ {result.get('dueAt')}")
            done_services.add(service)
            state[post_id] = sorted(done_services)
            save_json(STATE_PATH, state)
        except Exception as exc:
            failures.append(f"{service}: {exc}")

    if failures:
        raise RuntimeError("; ".join(failures))

    print(f"Done: {post_id} queued for X + Threads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

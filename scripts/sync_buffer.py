#!/usr/bin/env python3
"""Publish recent READY social posts from the repository to Buffer.

Each queue item may contain only X text, only Threads text, or both.
Threads is scheduled shortly after ingestion. X receives a deterministic
small delay so four daily X windows do not publish at identical minutes.
"""

from __future__ import annotations

import hashlib
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
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
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
    data = gql(
        api_key,
        f"""query {{ channels(input: {{ organizationId: \"{safe_org}\" }}) {{ id name service }} }}""",
    )
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
        {
            "input": {
                "text": text,
                "channelId": channel_id,
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due_at,
                "aiAssisted": True,
            }
        },
    )
    result = data["createPost"]
    if "message" in result and "post" not in result:
        raise RuntimeError(result["message"])
    return result["post"]


def twitter_delay_minutes(post_id: str, config: dict[str, Any]) -> int:
    minimum = int(config.get("twitter_publish_delay_min_minutes", 12))
    maximum = int(config.get("twitter_publish_delay_max_minutes", 47))
    if maximum < minimum:
        maximum = minimum
    span = maximum - minimum + 1
    digest = hashlib.sha256(post_id.encode("utf-8")).digest()
    jitter = int.from_bytes(digest[:4], "big") % span
    return minimum + jitter


def service_delay_minutes(service: str, post_id: str, config: dict[str, Any]) -> int:
    if service == "twitter":
        return twitter_delay_minutes(post_id, config)
    if service == "threads":
        return int(config.get("threads_publish_delay_minutes", 5))
    return int(config.get("publish_delay_minutes", 5))


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    configured_services = set(config.get("services", ["twitter", "threads"]))
    max_queue_age_hours = int(config.get("max_queue_age_hours", 12))

    posts = load_json(QUEUE_PATH, [])
    state: dict[str, list[str]] = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc)

    candidates: list[dict[str, Any]] = []
    for raw in posts:
        if str(raw.get("status", "")).upper() != "READY":
            continue
        if not raw.get("id") or not raw.get("created_at"):
            continue

        item = dict(raw)
        item["_created_at"] = parse_created_at(str(raw["created_at"]))
        age = now - item["_created_at"]
        if age > timedelta(hours=max_queue_age_hours):
            continue

        texts = {
            "twitter": str(raw.get("x") or "").strip(),
            "threads": str(raw.get("threads") or "").strip(),
        }
        available_services = {
            service
            for service, text in texts.items()
            if text and service in configured_services
        }
        if not available_services:
            continue

        done_services = set(state.get(str(raw["id"]), []))
        pending_services = available_services - done_services
        if not pending_services:
            continue

        item["_texts"] = texts
        item["_pending_services"] = pending_services
        candidates.append(item)

    if not candidates:
        print("No recent unpublished READY posts in repository queue.")
        return 0

    # Validate every pending item before sending anything.
    for post in candidates:
        post_id = str(post["id"])
        texts = post["_texts"]
        pending_services = post["_pending_services"]
        if "twitter" in pending_services and len(texts["twitter"]) > 280:
            raise RuntimeError(f"{post_id}: X text is over 280 characters")
        if "threads" in pending_services and len(texts["threads"]) > 500:
            raise RuntimeError(f"{post_id}: Threads text is over 500 characters")

    api_key = os.getenv("BUFFER_API_KEY", "").strip()
    if not api_key:
        print("ERROR: BUFFER_API_KEY is required.", file=sys.stderr)
        return 2

    organization_id = get_organization_id(api_key)
    channels = get_channels(api_key, organization_id)
    service_channels: dict[str, dict[str, Any]] = {}
    for channel in channels:
        service = channel.get("service")
        if service in configured_services and service not in service_channels:
            service_channels[service] = channel

    needed_services = set().union(*(post["_pending_services"] for post in candidates))
    missing = needed_services - set(service_channels)
    if missing:
        raise RuntimeError("Missing Buffer channel(s): " + ", ".join(sorted(missing)))

    failures: list[str] = []

    for post in sorted(candidates, key=lambda item: item["_created_at"]):
        post_id = str(post["id"])
        texts = post["_texts"]
        pending_services = set(post["_pending_services"])
        done_services = set(state.get(post_id, []))

        for service in sorted(pending_services):
            delay_minutes = service_delay_minutes(service, post_id, config)
            due_at = (
                datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")

            try:
                result = create_scheduled_post(
                    api_key,
                    service_channels[service]["id"],
                    texts[service],
                    due_at,
                )
                print(
                    f"CREATED {post_id} -> {service}: {result['id']} "
                    f"@ {result.get('dueAt')} (delay {delay_minutes}m)"
                )
                done_services.add(service)
                state[post_id] = sorted(done_services)
                save_json(STATE_PATH, state)
            except Exception as exc:
                failures.append(f"{post_id} -> {service}: {exc}")

    if failures:
        raise RuntimeError("; ".join(failures))

    print(f"Done. Processed {len(candidates)} queue item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

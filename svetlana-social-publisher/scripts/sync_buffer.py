#!/usr/bin/env python3
"""Sync approved posts from posts/posts.json to Buffer for X + Threads.

Safety defaults:
- config.json starts with dry_run=true.
- posts are ignored unless enabled=true.
- only posts within sync_horizon_days are considered.
- duplicate scheduled posts are skipped.
"""

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


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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
    data = gql(
        api_key,
        """
        query AccountOrganizations {
          account {
            organizations { id name }
          }
        }
        """,
    )
    orgs = data["account"]["organizations"]
    if not orgs:
        raise RuntimeError("No Buffer organization found.")
    if len(orgs) > 1:
        print(f"Found {len(orgs)} Buffer organizations; using: {orgs[0]['name']} ({orgs[0]['id']})")
    return orgs[0]["id"]


def get_channels(api_key: str, organization_id: str) -> list[dict[str, Any]]:
    # Organization IDs are server-provided opaque IDs; interpolate only this value.
    safe_org = organization_id.replace('"', '\\"')
    data = gql(
        api_key,
        f"""
        query Channels {{
          channels(input: {{ organizationId: \"{safe_org}\" }}) {{
            id
            name
            service
          }}
        }}
        """,
    )
    return data["channels"]


def get_scheduled_posts(api_key: str, organization_id: str) -> list[dict[str, Any]]:
    safe_org = organization_id.replace('"', '\\"')
    data = gql(
        api_key,
        f"""
        query ScheduledPosts {{
          posts(
            input: {{
              organizationId: \"{safe_org}\"
              sort: [{{ field: dueAt, direction: asc }}, {{ field: createdAt, direction: desc }}]
              filter: {{ status: [scheduled] }}
            }}
          ) {{
            edges {{
              node {{ id text dueAt channelId status }}
            }}
          }}
        }}
        """,
    )
    return [edge["node"] for edge in data["posts"]["edges"]]


def create_scheduled_post(api_key: str, channel_id: str, text: str, due_at: str) -> dict[str, Any]:
    data = gql(
        api_key,
        """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess {
              post { id text dueAt channelId status }
            }
            ... on MutationError {
              message
            }
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


def parse_utc(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("publish_at must include a timezone, preferably Z/UTC")
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_due(value: str | None) -> str:
    if not value:
        return ""
    return iso_z(parse_utc(value))


def main() -> int:
    config = load_json(ROOT / "config.json")
    posts = load_json(ROOT / "posts" / "posts.json")
    dry_run = bool(config.get("dry_run", True))
    horizon_days = int(config.get("sync_horizon_days", 5))
    wanted_services = set(config.get("services", ["twitter", "threads"]))

    api_key = os.getenv("BUFFER_API_KEY", "").strip()
    if not api_key:
        if dry_run:
            print("DRY RUN: BUFFER_API_KEY is not set. Validating local content only.")
        else:
            print("ERROR: BUFFER_API_KEY is required when dry_run=false.", file=sys.stderr)
            return 2

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)

    candidates: list[dict[str, Any]] = []
    for post in posts:
        if not post.get("enabled", False):
            continue
        when = parse_utc(post["publish_at"])
        if now <= when <= horizon:
            candidates.append({**post, "_when": when})

    if not candidates:
        print(f"Nothing to sync in the next {horizon_days} day(s).")
        return 0

    # Local-only validation works without an API key.
    for post in candidates:
        if post.get("x") and len(post["x"]) > 280:
            print(f"WARNING {post['id']}: X text is {len(post['x'])} chars; check account limits.")
        if not post.get("x") and not post.get("threads"):
            raise ValueError(f"{post['id']}: at least one of x/threads is required")

    if dry_run:
        for post in candidates:
            print(f"DRY RUN {post['id']} @ {iso_z(post['_when'])}")
            if post.get("x"):
                print("  X:", post["x"])
            if post.get("threads"):
                print("  Threads:", post["threads"])
        print("No posts were sent because config.json has dry_run=true.")
        return 0

    organization_id = get_organization_id(api_key)
    channels = get_channels(api_key, organization_id)
    service_channels: dict[str, dict[str, Any]] = {}
    for channel in channels:
        service = channel.get("service")
        if service in wanted_services and service not in service_channels:
            service_channels[service] = channel

    missing = wanted_services - set(service_channels)
    if missing:
        pretty = ", ".join(sorted(missing))
        raise RuntimeError(f"Missing Buffer channel(s): {pretty}. Connect them in Buffer first.")

    print("Connected channels:")
    for service, channel in sorted(service_channels.items()):
        print(f"  {service}: {channel['name']} ({channel['id']})")

    scheduled = get_scheduled_posts(api_key, organization_id)
    existing = {
        (item.get("channelId"), item.get("text", ""), normalize_due(item.get("dueAt")))
        for item in scheduled
    }

    created = 0
    skipped = 0
    mapping = {"twitter": "x", "threads": "threads"}

    for post in sorted(candidates, key=lambda p: p["_when"]):
        due_at = iso_z(post["_when"])
        for service in sorted(wanted_services):
            text_key = mapping[service]
            text = (post.get(text_key) or "").strip()
            if not text:
                continue
            channel_id = service_channels[service]["id"]
            fingerprint = (channel_id, text, due_at)
            if fingerprint in existing:
                print(f"SKIP {post['id']} -> {service}: already scheduled")
                skipped += 1
                continue
            result = create_scheduled_post(api_key, channel_id, text, due_at)
            print(f"CREATED {post['id']} -> {service}: {result['id']} @ {result.get('dueAt')}")
            existing.add(fingerprint)
            created += 1

    print(f"Done. Created: {created}; skipped duplicates: {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

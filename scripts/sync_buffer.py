#!/usr/bin/env python3
"""Publish the newest READY social post from Google Sheets to Buffer.

The sheet is the queue written by the ChatGPT automation.
Publishing state is stored in state/social_queue_state.json so the same row is not
published twice. If one service succeeds and the other fails, the next run retries
only the failed service.
"""

from __future__ import annotations

import csv
import io
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
STATE_PATH = ROOT / "state" / "social_queue_state.json"
DEFAULT_QUEUE_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "11PBrVIaXUWw00z2cbR5Qgc2wnjqsufVaoncL5eLV9GI/"
    "export?format=csv&gid=1570221331"
)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
          account { organizations { id name } }
        }
        """,
    )
    orgs = data["account"]["organizations"]
    if not orgs:
        raise RuntimeError("No Buffer organization found.")
    if len(orgs) > 1:
        print(f"Found {len(orgs)} Buffer organizations; using {orgs[0]['name']}.")
    return orgs[0]["id"]


def get_channels(api_key: str, organization_id: str) -> list[dict[str, Any]]:
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


def create_scheduled_post(api_key: str, channel_id: str, text: str, due_at: str) -> dict[str, Any]:
    data = gql(
        api_key,
        """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess {
              post { id text dueAt channelId status }
            }
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


def parse_created_at(value: str) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_queue(csv_url: str) -> list[dict[str, str]]:
    separator = "&" if "?" in csv_url else "?"
    fresh_url = f"{csv_url}{separator}_ts={int(datetime.now(timezone.utc).timestamp())}"
    response = requests.get(
        fresh_url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=30,
    )
    response.raise_for_status()
    return list(csv.DictReader(io.StringIO(response.text)))


def main() -> int:
    config = load_json(CONFIG_PATH, {})
    wanted_services = set(config.get("services", ["twitter", "threads"]))
    queue_csv_url = config.get("queue_csv_url", DEFAULT_QUEUE_CSV_URL)
    max_queue_age_hours = int(config.get("max_queue_age_hours", 8))
    publish_delay_minutes = int(config.get("publish_delay_minutes", 5))

    unsupported = wanted_services - {"twitter", "threads"}
    if unsupported:
        raise RuntimeError(f"Unsupported service(s): {', '.join(sorted(unsupported))}")

    rows = load_queue(queue_csv_url)
    ready_rows: list[dict[str, str]] = []

    for row in rows:
        if (row.get("status") or "").strip().upper() != "READY":
            continue
        if not (row.get("id") or "").strip():
            continue
        try:
            row["_created_at"] = parse_created_at(row.get("created_at") or "")  # type: ignore[assignment]
        except Exception as exc:
            print(f"SKIP invalid queue row {row.get('id')}: bad created_at ({exc})")
            continue
        ready_rows.append(row)

    if not ready_rows:
        print("No READY rows in queue.")
        return 0

    row = max(ready_rows, key=lambda item: item["_created_at"])  # type: ignore[index]
    post_id = (row.get("id") or "").strip()
    created_at = row["_created_at"]  # type: ignore[index]

    age = datetime.now(timezone.utc) - created_at
    if age > timedelta(hours=max_queue_age_hours):
        print(
            f"Newest READY row {post_id} is {age.total_seconds() / 3600:.1f}h old; "
            "refusing to publish stale content."
        )
        return 0

    texts = {
        "twitter": (row.get("x_text") or "").strip(),
        "threads": (row.get("threads_text") or "").strip(),
    }

    for service in wanted_services:
        if not texts.get(service):
            raise RuntimeError(f"{post_id}: missing text for {service}")

    # Validate every platform before sending anything. This prevents a partial
    # publish where X succeeds and Threads fails on a platform limit.
    if "twitter" in wanted_services and len(texts["twitter"]) > 280:
        raise RuntimeError(
            f"{post_id}: X text is {len(texts['twitter'])} characters; refusing to publish over 280."
        )
    if "threads" in wanted_services and len(texts["threads"]) > 500:
        raise RuntimeError(
            f"{post_id}: Threads text is {len(texts['threads'])} characters; refusing to publish over 500."
        )

    api_key = os.getenv("BUFFER_API_KEY", "").strip()
    if not api_key:
        print("ERROR: BUFFER_API_KEY is required.", file=sys.stderr)
        return 2

    state: dict[str, list[str]] = load_json(STATE_PATH, {})
    done_services = set(state.get(post_id, []))

    if wanted_services.issubset(done_services):
        print(f"{post_id}: already published to all configured services.")
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
        raise RuntimeError(
            "Missing Buffer channel(s): " + ", ".join(sorted(missing))
        )

    print("Connected Buffer channels:")
    for service in sorted(wanted_services):
        channel = service_channels[service]
        print(f"  {service}: {channel['name']} ({channel['id']})")

    failures: list[str] = []

    for service in sorted(wanted_services):
        if service in done_services:
            print(f"SKIP {post_id} -> {service}: already recorded as published")
            continue

        due_at = (
            datetime.now(timezone.utc) + timedelta(minutes=publish_delay_minutes)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        try:
            result = create_scheduled_post(
                api_key,
                service_channels[service]["id"],
                texts[service],
                due_at,
            )
            print(
                f"CREATED {post_id} -> {service}: "
                f"{result['id']} @ {result.get('dueAt')}"
            )
            done_services.add(service)
            state[post_id] = sorted(done_services)
            save_json(STATE_PATH, state)
        except Exception as exc:
            failures.append(f"{service}: {exc}")

    state[post_id] = sorted(done_services)
    save_json(STATE_PATH, state)

    if failures:
        raise RuntimeError("; ".join(failures))

    print(f"Done: {post_id} queued for X + Threads.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

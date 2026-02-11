"""
End-to-end test for the Klime CDP destination template.

Compiles the Hog template and executes it with real HTTP calls against
the Klime ingest API. Requires a valid Klime write key.

Usage:
    .venv/bin/python posthog/cdp/templates/klime/e2e_test_klime.py <write_key> [--endpoint http://ingest.klime.dev]
"""

import os
import sys
import json
import uuid
import logging
import argparse
import warnings
from datetime import UTC, datetime

# Suppress noisy Django startup warnings
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "posthog.settings")
os.environ.setdefault("SECRET_KEY", "e2e-test-not-production")
os.environ.setdefault("DEBUG", "1")
# Use whatever Postgres is available — the script only needs Django to initialize, not a real posthog DB
os.environ.setdefault("PGUSER", "postgres")
os.environ.setdefault("PGPASSWORD", "postgres")

_stdout, _stderr = sys.stdout, sys.stderr
_devnull = open(os.devnull, "w")
sys.stdout = sys.stderr = _devnull
import django

django.setup()
_devnull.close()
sys.stdout, sys.stderr = _stdout, _stderr

import atexit

atexit._clear()  # type: ignore[attr-defined]

import requests

from posthog.cdp.templates.klime.template_klime import template
from posthog.cdp.validation import compile_hog

from common.hogvm.python.execute import execute_bytecode


def real_fetch(url, options=None):
    method = (options.get("method", "GET") if options else "GET").upper()
    headers = options.get("headers", {}) if options else {}
    body = options.get("body") if options else None

    try:
        response = requests.request(
            method=method, url=url, headers=headers, json=body, verify=url.startswith("https://ingest.klime.com")
        )
    except requests.exceptions.ConnectionError as e:
        return {"status": 502, "body": f"Connection error: {e}"}

    try:
        response_body = response.json()
    except Exception:
        response_body = response.text

    return {"status": response.status_code, "body": response_body}


def run_e2e(write_key: str, endpoint: str):
    # Patch the hardcoded endpoint if a different one is requested
    code = template.code
    if endpoint != "https://ingest.klime.com":
        code = code.replace("https://ingest.klime.com", endpoint)

    bytecode = compile_hog(code, template.type)
    now = datetime.now(UTC).isoformat()
    logs = []

    def log_print(*args):
        logs.append(args)
        print(f"  [hog print] {args}")

    def run(label: str, inputs: dict, event: dict, person: dict | None = None):
        logs.clear()
        print(f"\n--- {label} ---")
        globals = {
            "event": {
                "uuid": str(uuid.uuid4()),
                "event": "test",
                "distinct_id": "e2e-test-user",
                "properties": {},
                "timestamp": now,
                "elements_chain": "",
                **event,
            },
            "person": {"id": "person-e2e", "properties": {}, **(person or {})},
            "source": {"url": "https://e2e-test"},
            "inputs": {
                "writeKey": write_key,
                "action": "automatic",
                "userId": "e2e-test-user",
                "groupId": "",
                "include_all_properties": False,
                "properties": {},
                **inputs,
            },
        }

        fetch_calls = []
        original_fetch = real_fetch

        def tracking_fetch(url, options=None):
            result = original_fetch(url, options)
            fetch_calls.append({"url": url, "status": result["status"], "body": result["body"]})
            return result

        try:
            execute_bytecode(
                bytecode,
                globals,
                functions={"fetch": tracking_fetch, "print": log_print},
            )
        except Exception as e:
            print(f"  [ERROR] {e}")

        if fetch_calls:
            for call in fetch_calls:
                status = call["status"]
                ok = "OK" if status < 400 else "FAIL"
                print(f"  [{ok}] {call['url']} -> {status}")
                print(f"  Response: {json.dumps(call['body'], indent=2)}")
        elif logs:
            print("  (no HTTP call made — skipped by Hog code)")
        else:
            print("  (no output)")

        return fetch_calls

    print(f"Klime E2E test against {endpoint}")
    print(f"Write key: {write_key[:8]}...")

    # 1. Track event
    run(
        "Track event",
        inputs={},
        event={"event": "E2E Button Clicked", "properties": {"button": "signup", "page": "/pricing"}},
    )

    # 2. Identify event (automatic detection via $identify)
    run(
        "Identify event (automatic)",
        inputs={"include_all_properties": True},
        event={"event": "$identify"},
        person={"properties": {"email": "e2e@klime.com", "name": "E2E Test", "$creator_event_uuid": "x"}},
    )

    # 3. Group event (automatic detection via $group_identify)
    run(
        "Group event (automatic)",
        inputs={"groupId": "e2e-org-123", "properties": {"name": "E2E Org", "plan": "enterprise"}},
        event={"event": "$group_identify"},
    )

    # 4. Track with include_all_properties
    run(
        "Track with all properties (filters $-prefixed)",
        inputs={"include_all_properties": True},
        event={"event": "E2E Purchase", "properties": {"$lib": "web", "amount": 99.99, "currency": "USD"}},
    )

    # 5. Identify without userId (should skip)
    run(
        "Identify without userId (should skip)",
        inputs={"action": "identify", "userId": ""},
        event={"event": "$identify"},
    )

    # 6. Auth failure (bad key)
    print("\n--- Auth failure (bad write key) ---")
    bad_globals = {
        "event": {
            "uuid": str(uuid.uuid4()),
            "event": "test",
            "distinct_id": "x",
            "properties": {},
            "timestamp": now,
            "elements_chain": "",
        },
        "person": {"id": "p", "properties": {}},
        "source": {"url": "https://e2e-test"},
        "inputs": {
            "writeKey": "invalid-key-12345",
            "action": "track",
            "userId": "x",
            "groupId": "",
            "include_all_properties": False,
            "properties": {},
        },
    }
    try:
        execute_bytecode(
            bytecode,
            bad_globals,
            functions={"fetch": real_fetch, "print": log_print},
        )
        print("  [UNEXPECTED] No error raised")
    except Exception as e:
        print(f"  [OK] Error raised: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E test for Klime CDP destination")
    parser.add_argument("write_key", help="Klime write key")
    parser.add_argument(
        "--endpoint", default="http://ingest.klime.dev", help="Klime API endpoint (default: http://ingest.klime.dev)"
    )
    args = parser.parse_args()
    run_e2e(args.write_key, args.endpoint)

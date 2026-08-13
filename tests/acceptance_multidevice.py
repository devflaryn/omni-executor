#!/usr/bin/env python3
"""Multi-device acceptance check, run against a LIVE server.

Not a unit test: it signs real users into a real deployment and asserts the
privacy wall from the outside, the way two actual machines meet it. Run the
same file on each machine.

    # machine 1, as the owner
    python tests/acceptance_multidevice.py owner   --email a@b.c --password ... \
        --mine admn1b12farm2 --theirs admn1b12farm4

    # machine 2, as a different user who owns `theirs`
    python tests/acceptance_multidevice.py other   --email d@e.f --password ... \
        --mine admn1b12farm4

What it proves, in the order it matters:

  1. an account's cookie follows the USER, not the machine
  2. presence names the device an account is running on
  3. a user cannot execute against an account they do not own
  4. a user cannot even read the status or results of one
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cloud  # noqa: E402
import accountsync  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return ok


def role_owner(args):
    print(f"\n== signed in as {args.email} on {cloud.device()['deviceName']} ==")
    me = cloud.me()
    check("plan is active", bool(me["subscription"]["active"]),
          me["subscription"].get("planLabel"))

    mine = {a["username"]: a for a in cloud.list_accounts()}
    check(f"{args.mine} is mine", args.mine in mine)
    check(f"{args.mine} has a stored cookie", mine.get(args.mine, {}).get("hasCookie", False))
    check(f"{args.theirs} is NOT mine", args.theirs not in mine)

    # 1. the cookie follows the user
    try:
        cookie = cloud.get_cookie(args.mine)
        check("I can read my own cookie", bool(cookie), f"{len(cookie)} chars")
    except cloud.CloudError as e:
        check("I can read my own cookie", False, e.message)
    try:
        cloud.get_cookie(args.theirs)
        check("I CANNOT read someone else's cookie", False, "it was returned!")
    except cloud.CloudError as e:
        check("I CANNOT read someone else's cookie", e.status in (403, 404),
              f"{e.status} {e.message}")

    # 2. presence
    for name, row in mine.items():
        print(f"      presence: {name} -> {row['presence']['label']}")

    # 3. execution
    try:
        r = cloud.request("POST", "/omni/exec/submit",
                          {"channel": args.mine, "script": 'print("omni")'})
        check("I can submit to my own account", bool(r.get("id")),
              f"connected={r.get('connected')}")
    except cloud.CloudError as e:
        check("I can submit to my own account", False, f"{e.status} {e.message}")

    try:
        cloud.request("POST", "/omni/exec/submit",
                      {"channel": args.theirs, "script": 'print("pwned")'})
        check("I CANNOT execute in someone else's account", False,
              "the submit was ACCEPTED")
    except cloud.CloudError as e:
        check("I CANNOT execute in someone else's account",
              e.status == 403 and e.error == "not_your_account",
              f"{e.status} {e.error}")

    # 4. not even status
    try:
        cloud.request("GET", f"/omni/exec/status?channel={args.theirs}")
        check("I CANNOT see someone else's session status", False, "it answered")
    except cloud.CloudError as e:
        check("I CANNOT see someone else's session status", e.status == 403,
              f"{e.status} {e.error}")


def role_other(args):
    print(f"\n== signed in as {args.email} on {cloud.device()['deviceName']} ==")
    mine = {a["username"]: a for a in cloud.list_accounts()}
    check(f"{args.mine} is mine", args.mine in mine)
    check("I do not see the other user's accounts",
          all(n == args.mine for n in mine), ", ".join(mine))
    try:
        r = cloud.request("POST", "/omni/exec/submit",
                          {"channel": args.mine, "script": 'print("mine")'})
        check("I can submit to my own account", bool(r.get("id")))
    except cloud.CloudError as e:
        check("I can submit to my own account", False, f"{e.status} {e.message}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["owner", "other"])
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--mine", required=True)
    ap.add_argument("--theirs", default=None)
    ap.add_argument("--sync", action="store_true",
                    help="pull this user's accounts down first")
    args = ap.parse_args()

    print(f"server: {cloud.api_base()}")
    try:
        cloud.login(args.email, args.password)
    except cloud.CloudError as e:
        sys.exit(f"sign-in failed: {e.message}")

    if args.sync:
        r = accountsync.sync()
        print(f"  sync: {len(r['pushed'])} up, {len(r['pulled'])} down, "
              f"{len(r['skipped'])} skipped")

    (role_owner if args.role == "owner" else role_other)(args)

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

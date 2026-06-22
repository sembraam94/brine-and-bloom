#!/usr/bin/env python3
"""
Refresh the long-lived Instagram access token.

Instagram User tokens (Instagram Login flow) last ~60 days. Calling the refresh
endpoint resets the clock to another 60 days. Refresh needs NO app secret — just
the current token — but the token must be at least 24h old and not yet expired.
The companion workflow (.github/workflows/refresh-token.yml) runs this weekly so
the account never silently goes dark.

Writes the new token to the path given by --out (so a workflow can store it in a
secret without the value ever being printed to logs). Never prints the token.

Env: IG_ACCESS_TOKEN
"""

import os
import sys
import requests

from autopost import GRAPH_HOST


def main():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        sys.exit("Missing IG_ACCESS_TOKEN")

    out_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("--out="):
            out_path = arg.split("=", 1)[1]

    resp = requests.get(
        f"{GRAPH_HOST}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    new_token = data.get("access_token")
    if not new_token:
        sys.exit(f"No access_token in refresh response: {resp.text}")

    days = int(data.get("expires_in", 0)) // 86400
    print(f"Token refreshed OK; valid ~{days} more days.", file=sys.stderr)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(new_token)
        print(f"New token written to {out_path}.", file=sys.stderr)
    else:
        # No --out: don't print the token. Just confirm success.
        print("(no --out given; token not written anywhere)", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Binary PVE upload: neither curl's argv nor a credential file holds a ticket."""
import json
import re
import subprocess
import sys


def upload(config):
    for key in ("url", "ticket", "csrf", "path", "filename"):
        if not isinstance(config[key], str) or any(c in config[key] for c in "\r\n\0"):
            raise ValueError("Invalid upload configuration")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.iso", config["filename"]):
        raise ValueError("Invalid ISO upload filename")
    path = config["path"].replace("\\", "\\\\").replace('"', '\\"')
    options = {
        "url": config["url"],
        "header": "Cookie: PVEAuthCookie=" + config["ticket"],
    }
    lines = [key + " = " + json.dumps(value) for key, value in options.items()]
    lines += [
        "header = " + json.dumps("CSRFPreventionToken: " + config["csrf"]),
        'form = "content=iso"',
        "form = " + json.dumps('filename=@"' + path + '";filename=' + config["filename"]),
        "max-time = 3600",
    ]
    if not config["validate_certs"]:
        lines.append("insecure")
    result = subprocess.run(
        ["curl", "--disable", "--fail-with-body", "--silent", "--show-error", "--config", "-"],
        input="\n".join(lines) + "\n", text=True, capture_output=True, check=True,
    )
    response = json.loads(result.stdout)
    upid = response.get("data") if isinstance(response, dict) else None
    if not isinstance(upid, str) or not upid.startswith("UPID:"):
        raise ValueError("Proxmox upload returned no task UPID")
    return upid


if __name__ == "__main__":
    try:
        print(json.dumps({"data": upload(json.load(sys.stdin))}))
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        # curl stderr can include headers/URLs; never print the configuration.
        print("Proxmox upload failed (" + type(error).__name__ + ")", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Select the most capable QEMU CPU model safe across online Proxmox cluster nodes."""
import json
import re
import sys

_V2 = frozenset({"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"})
_V3_EXTRA = frozenset({"avx", "avx2", "bmi1", "bmi2", "fma", "movbe", "xsave"})
_V4_EXTRA = frozenset({"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"})


def select_cpu_type(nodes_cpuinfo, override=""):
    override = (override or "").strip()
    if override and override.lower() != "auto":
        if not re.fullmatch(r"[A-Za-z0-9_,=+-]+", override):
            raise ValueError(f"Invalid SOPHON_PROXMOX_CPU_TYPE: {override!r}")
        return override

    profiles = []
    for info in nodes_cpuinfo or []:
        if not isinstance(info, dict):
            continue
        flags = frozenset(str(info.get("flags") or "").split())
        if not flags:
            continue
        model = str(info.get("model") or "").strip()
        profiles.append((model, flags))

    if not profiles:
        return "x86-64-v2-AES"

    # Single-node host or homogeneous cluster (identical CPU model and flags on all online nodes):
    # pass through native host features without breaking intra-cluster live migration.
    if all(model for model, _ in profiles) and len(set(profiles)) == 1:
        return "host"

    common = frozenset.intersection(*(flags for _, flags in profiles))
    has_v3_bitmanip = all(("abm" in flags or "lzcnt" in flags) for _, flags in profiles)
    if _V2 <= common and _V3_EXTRA <= common and has_v3_bitmanip:
        if _V4_EXTRA <= common:
            return "x86-64-v4"
        return "x86-64-v3"
    if (_V2 | {"aes"}) <= common:
        return "x86-64-v2-AES"
    if _V2 <= common:
        return "x86-64-v2"
    return "kvm64"


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    print(json.dumps({
        "cpu_type": select_cpu_type(
            payload.get("nodes") or [],
            payload.get("override") or "",
        )
    }))

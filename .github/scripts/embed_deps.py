#!/usr/bin/env python3
"""Embed HBM (NTM - Continuum) and Golem Core into every NTM: Space jar (jar-in-jar).

Takes a dist directory of flat release jars (all renamed with a -build.N suffix), matches each
ntmspace-<loader>-<version>+<mc>-build.N.jar with the hbm- and golemcore- jars of the same loader
and Minecraft version, and writes a self-contained copy per Minecraft version to
<out>/<mc>/ntmspace-<loader>-<version>+<mc>-all-in-one-build.N.jar.

The embedding follows each loader's own standard, so the mod loaders resolve the nested mods
normally (no shading, no class rewriting):
- Fabric: nested jars under META-INF/jars/ plus a "jars" entry in fabric.mod.json.
- Forge and NeoForge: nested jars under META-INF/jarjar/ plus META-INF/jarjar/metadata.json
  (JarJar). The 1.20.1 Forge jars are the obfuscated (reobf) ones, so their entries are
  marked "isObfuscated": true; NeoForge 1.21.1 and 26.1 jars are not obfuscated.

Usage: embed_deps.py <dist-dir> <out-dir>
"""

import json
import re
import sys
import zipfile
from pathlib import Path

# ntmspace-fabric-1.0.3+1.21.1-build.2.jar / hbm-forge-1.0.6+1.20.1-reobf-build.2.jar
NAME_RE = re.compile(
    r"^(?P<mod>ntmspace|hbm|golemcore)"
    r"-(?P<loader>fabric|forge|neoforge)"
    r"-(?P<modversion>.+?)\+"
    r"(?P<mc>[^-]+?)"
    r"(?P<reobf>-reobf)?"
    r"-build\.(?P<build>\d+)\.jar$"
)

# JarJar coordinates of the two dependencies (must match what the composite builds publish).
DEPS = {
    "hbm": {"group": "com.hbm", "artifact": "hbm"},
    "golemcore": {"group": "dev.golem", "artifact": "golemcore"},
}

# Order of the per-Minecraft-version releases the workflow publishes.
MC_ORDER = ["1.21.1", "1.20.1", "26.1"]


def parse_name(path):
    m = NAME_RE.match(path.name)
    if not m:
        return None
    info = {
        "path": path,
        "mod": m["mod"],
        "loader": m["loader"],
        "version": m["modversion"],
        # The same Minecraft version with and without -reobf must group together.
        "mc": m["mc"],
        "build": m["build"],
        "reobf": m["reobf"] is not None,
    }
    return info


def nested_entry_name(dep, obfuscated):
    """Name of the dependency jar inside the jar-in-jar directories."""
    suffix = "-reobf" if obfuscated else ""
    return f"{dep['artifact']}-{dep['version']}{suffix}.jar"


def read_entry(zf, entry):
    return zf.read(entry)


def build_jarjar_metadata(existing, entries):
    metadata = {"jars": []}
    if existing is not None:
        try:
            parsed = json.loads(existing.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("jars"), list):
                metadata["jars"] = parsed["jars"]
        except (ValueError, UnicodeDecodeError):
            print("  warning: unparsable existing jarjar metadata, starting fresh")
    paths = {jar.get("path") for jar in metadata["jars"]}
    for entry in entries:
        spec = {
            "identifier": {"group": entry["group"], "artifact": entry["artifact"]},
            "version": {"range": f"[{entry['version']},)", "artifactVersion": entry["version"]},
            "isObfuscated": entry["obfuscated"],
            "path": entry["path"],
        }
        action = "replaces existing entry" if spec["path"] in paths else "added"
        metadata["jars"] = [j for j in metadata["jars"] if j.get("path") != spec["path"]]
        metadata["jars"].append(spec)
        print(f"  jarjar {entry['artifact']} {entry['version']} ({action})")
    return json.dumps(metadata, indent=2).encode("utf-8") + b"\n"


def embed_fabric(zf, dep_entries, out):
    """Fabric jar-in-jar: META-INF/jars/ + fabric.mod.json "jars"."""
    adds = {}
    jarmod_entries = []
    for entry in dep_entries:
        path = f"META-INF/jars/{entry['nested']}"
        adds[path] = entry["bytes"]
        jarmod_entries.append({"file": path})
        print(f"  fabric nested jar {path}")

    existing = json.loads(read_entry(zf, "fabric.mod.json").decode("utf-8"))
    jars = existing.get("jars")
    if not isinstance(jars, list):
        jars = []
    before = len(jars)
    have = {j.get("file") for j in jars if isinstance(j, dict)}
    for jarmod in jarmod_entries:
        if jarmod["file"] not in have:
            jars.append(jarmod)
    existing["jars"] = jars
    adds["fabric.mod.json"] = json.dumps(existing, indent=2).encode("utf-8") + b"\n"
    print(f"  fabric.mod.json jars: {before} -> {len(jars)}")
    return adds


def embed_fml(zf, dep_entries, out):
    """Forge/NeoForge JarJar: META-INF/jarjar/ + metadata.json."""
    existing = None
    try:
        existing = read_entry(zf, "META-INF/jarjar/metadata.json")
    except KeyError:
        pass
    adds = {f"META-INF/jarjar/{e['nested']}": e["bytes"] for e in dep_entries}
    adds["META-INF/jarjar/metadata.json"] = build_jarjar_metadata(
        existing,
        [
            {
                "group": e["group"],
                "artifact": e["artifact"],
                "version": e["version"],
                "obfuscated": e["obfuscated"],
                "path": f"META-INF/jarjar/{e['nested']}",
            }
            for e in dep_entries
        ],
    )
    return adds


def repack(src_path, dest_path, adds):
    with zipfile.ZipFile(src_path) as zin:
        names = zin.namelist()
        skip = set(adds)
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name.endswith("/") or name in skip:
                    continue
                zout.writestr(name, zin.read(name))
            for name, data in adds.items():
                zout.writestr(name, data)


def main():
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <dist-dir> <out-dir>")
    dist = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    jars = []
    for path in sorted(dist.glob("*.jar")):
        info = parse_name(path)
        if info:
            jars.append(info)
        else:
            print(f"ignored: {path.name}")

    # Dependency jars keyed by (loader, mc); the -reobf suffix identifies obfuscated artifacts.
    deps = {}
    for info in jars:
        if info["mod"] == "ntmspace":
            if info["reobf"]:
                sys.exit(f"error: the NTM: Space release jar itself must not be reobf: {info['path'].name}")
            continue
        key = (info["loader"], info["mc"])
        slot = "reobf" if info["reobf"] else "normal"
        deps.setdefault(key, {})[info["mod"]] = {
            "info": info,
            "obfuscated": slot == "reobf",
        }

    builds = {}
    for info in jars:
        if info["mod"] != "ntmspace":
            continue
        key = (info["loader"], info["mc"])
        found = deps.get(key, {})
        if set(found) != set(DEPS):
            missing = sorted(set(DEPS) - set(found))
            sys.exit(
                f"error: no matching dependency jar for {info['path'].name} (missing: {', '.join(missing) or 'none'})"
            )
        dep_entries = []
        for mod, meta in DEPS.items():
            dep = found[mod]
            obfuscated = dep["obfuscated"] or info["loader"] == "forge"
            dep_entries.append(
                {
                    **meta,
                    "version": dep["info"]["version"],
                    "obfuscated": obfuscated,
                    "nested": nested_entry_name({"artifact": meta["artifact"], "version": dep["info"]["version"]}, obfuscated),
                    "bytes": dep["info"]["path"].read_bytes(),
                }
            )
        dest_name = info["path"].name.replace("-build." + info["build"], f"-all-in-one-build.{info['build']}")
        dest = out / info["mc"] / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"{info['path'].name} -> {dest.relative_to(out)}")
        with zipfile.ZipFile(info["path"]) as zf:
            if info["loader"] == "fabric":
                adds = embed_fabric(zf, dep_entries, dest)
            else:
                adds = embed_fml(zf, dep_entries, dest)
        repack(info["path"], dest, adds)
        builds.setdefault(info["mc"], []).append(dest)

    if not builds:
        sys.exit("error: no NTM: Space jars found to embed into")
    for mc in sorted(builds, key=lambda v: MC_ORDER.index(v) if v in MC_ORDER else 99):
        for jar in builds[mc]:
            print(f"all-in-one: {jar.relative_to(out)} ({jar.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()

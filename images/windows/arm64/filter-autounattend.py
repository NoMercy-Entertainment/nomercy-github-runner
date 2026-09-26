#!/usr/bin/env python3
"""Keep only ARM64 components from an existing Windows answer file."""

import argparse
import xml.etree.ElementTree as ET

UNATTEND = "urn:schemas-microsoft-com:unattend"
WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()

    ET.register_namespace("", UNATTEND)
    ET.register_namespace("wcm", WCM)
    tree = ET.parse(args.source)
    root = tree.getroot()
    removed = []
    for settings in root.findall(f"{{{UNATTEND}}}settings"):
        for component in list(settings):
            arch = component.get("processorArchitecture")
            if arch and arch.lower() != "arm64":
                removed.append((settings.get("pass"), component.get("name"), arch))
                settings.remove(component)
    if not removed:
        raise SystemExit("No non-ARM64 components found; refusing unchanged copy")
    tree.write(args.target, encoding="utf-8", xml_declaration=True)
    check = ET.parse(args.target)
    if any(
        c.get("processorArchitecture", "arm64").lower() != "arm64"
        for c in check.getroot().iter(f"{{{UNATTEND}}}component")
    ):
        raise SystemExit("Filtered answer file still has non-ARM64 components")
    print(f"Removed {len(removed)} non-ARM64 components; kept ARM64 settings")


if __name__ == "__main__":
    main()

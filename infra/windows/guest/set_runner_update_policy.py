"""Keep Windows runner VM updates in explicit maintenance windows.

Run with the guest agent's native Python, as administrator. This changes only
Windows Update policy, never services, pagefiles, memory, disks or boot state.
"""
import argparse
import json
import os
from pathlib import Path

AU = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
WU = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
POLICY = {"NoAutoUpdate": 1, "AUOptions": 2,
          "NoAutoRebootWithLoggedOnUsers": 1, "AlwaysAutoRebootAtScheduledTime": 0}
DEADLINES = {"SetComplianceDeadline", "ConfigureDeadlineForQualityUpdates",
             "ConfigureDeadlineForFeatureUpdates", "ConfigureDeadlineGracePeriod"}


def read_values(registry, path):
    try:
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, path) as key:
            values = {}
            index = 0
            while True:
                try:
                    name, value, kind = registry.EnumValue(key, index)
                except OSError as error:
                    if getattr(error, "winerror", None) != 259:
                        raise
                    break
                values[name] = {"value": value, "type": kind}
                index += 1
            return values
    except FileNotFoundError:
        return {}


def apply_policy(registry, root, expected, computer, apply=False):
    if expected != "rnr-windows-1" or computer.upper() == "BEAST-UNIT":
        raise RuntimeError("Only the enrolled Windows runner VM is eligible")
    config = json.loads((root / "agent.json").read_text(encoding="utf-8-sig"))
    if config.get("host_id") != expected:
        raise RuntimeError("The guest agent identity does not match the selected worker")
    bios = read_values(registry, r"HARDWARE\DESCRIPTION\System\BIOS")
    model = " ".join(str(bios.get(k, {}).get("value", "")) for k in ("SystemManufacturer", "SystemProductName"))
    if not any(marker in model.lower() for marker in ("qemu", "virtual machine", "kvm", "virt-")):
        raise RuntimeError("The target is not a recognized virtual machine")
    current, updates = read_values(registry, AU), read_values(registry, WU)
    if any(key in updates for key in DEADLINES):
        raise RuntimeError("A configured update deadline needs review before changing update policy")
    result = {"worker": expected, "computer": computer, "model": model,
              "policy": {name: current.get(name, {}).get("value") for name in POLICY}}
    if apply:
        backup = root / "update-policy-before-manual-maintenance.json"
        if not backup.exists():
            with backup.open("x", encoding="utf-8") as destination:
                json.dump({"worker": expected, "computer": computer, "values": current}, destination, indent=2)
        with registry.CreateKeyEx(registry.HKEY_LOCAL_MACHINE, AU, 0, registry.KEY_SET_VALUE) as key:
            for name, value in POLICY.items():
                registry.SetValueEx(key, name, 0, registry.REG_DWORD, value)
        checked = read_values(registry, AU)
        if any(checked.get(name, {}).get("value") != value for name, value in POLICY.items()):
            raise RuntimeError("Update policy readback failed")
        result.update(policy=POLICY, backup=str(backup), applied=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, choices=("rnr-windows-1",))
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    import winreg
    root = Path(r"C:\ProgramData\nomercy\agent")
    print(json.dumps(apply_policy(winreg, root, arguments.worker, os.environ.get("COMPUTERNAME", ""), arguments.apply)), flush=True)


if __name__ == "__main__":
    main()

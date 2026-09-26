"""Send one QEMU HMP command to the local Windows ARM guest."""

import argparse
import re
import socket
import time


def until_prompt(conn):
    data = bytearray()
    while not data.endswith(b"(qemu) "):
        chunk = conn.recv(4096)
        if not chunk:
            raise RuntimeError("QEMU monitor closed the connection")
        data.extend(chunk)
    return data.decode(errors="replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("info status", "screendump", "type", "key",
                                             "attach-payload", "forward-agent"))
    parser.add_argument("output", nargs="?")
    parser.add_argument("--no-enter", action="store_true",
                        help="leave typed text in the guest without pressing Enter")
    args = parser.parse_args()
    command = args.command
    if command == "screendump":
        if not args.output or not args.output.startswith("/tmp/"):
            parser.error("screendump needs an absolute /tmp/ output path")
        command += " " + args.output
    if command == "type" and not args.output:
        parser.error("type needs text to send to the guest console")
    if command == "key" and not (args.output and re.fullmatch(r"[a-z0-9-]+", args.output)):
        parser.error("key needs a QEMU key name or combination")
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect("/var/lib/runner-appliances/windows-arm64/monitor.sock")
        until_prompt(conn)
        if command == "type":
            keys = {":": "shift-semicolon", "\\": "backslash", ".": "dot",
                    " ": "spc", "-": "minus", "+": "shift-equal"}
            for char in args.output.lower():
                key = keys.get(char, char)
                if not (char.isascii() and (char.isalnum() or char in keys)):
                    parser.error(f"unsupported console character: {char!r}")
                conn.sendall((f"sendkey {key}\n").encode())
                until_prompt(conn)
                time.sleep(0.12)
            if not args.no_enter:
                conn.sendall(b"sendkey ret\n")
                until_prompt(conn)
                if args.output.lower().endswith("bootaa64.efi"):
                    time.sleep(1)
                    conn.sendall(b"sendkey a\n")
                    until_prompt(conn)
            print("sent text to the guest console" +
                  ("" if args.no_enter else " and pressed Enter"))
        elif command == "key":
            conn.sendall((f"sendkey {args.output}\n").encode())
            print(until_prompt(conn))
        else:
            commands = {
                "attach-payload": [
                    "drive_add 0 if=none,id=payload,format=raw,media=cdrom,readonly=on,file=/var/lib/runner-appliances/windows-arm64/payload.iso",
                    "device_add usb-storage,drive=payload,id=arm_payload",
                ],
                "forward-agent": [
                    "hostfwd_add net0 tcp:172.19.136.46:8445-:8443",
                ],
            }.get(command, [command])
            for item in commands:
                conn.sendall((item + "\n").encode())
                answer = until_prompt(conn)
                if "Error:" in answer or "invalid" in answer.lower():
                    raise RuntimeError(answer)
                print(answer)


if __name__ == "__main__":
    main()

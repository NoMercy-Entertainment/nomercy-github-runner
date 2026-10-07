#!/usr/bin/env python3
"""Remove x86/amd64 components without rewriting the remaining answer file."""

import argparse
from copy import deepcopy
from pathlib import Path
from xml.parsers import expat
import xml.etree.ElementTree as ET

UNATTEND = "urn:schemas-microsoft-com:unattend"


def filter_answer(source: bytes) -> tuple[bytes, int]:
    # Byte ranges preserve comments, namespaces, CDATA, scripts and formatting.
    # Do not silently rewrite a UTF-16 answer file as UTF-8.
    if b'\x00' in source:
        raise ValueError('Expected a UTF-8 answer file')
    parser = expat.ParserCreate(namespace_separator='}')
    stack = []
    ranges = []

    def start(name, attributes):
        remove = (len(stack) == 2 and
                  stack[-1][0] == UNATTEND + '}settings' and
                  name == UNATTEND + '}component' and
                  attributes.get('processorArchitecture', '').lower() in ('x86', 'amd64'))
        stack.append((name, parser.CurrentByteIndex if remove else None))

    def end(name):
        _, first = stack.pop()
        if first is not None:
            last = parser.CurrentByteIndex
            if source[last:last + 2] == b'</':
                last = source.index(b'>', last) + 1
            ranges.append((first, last))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(source, True)
    if not ranges:
        raise ValueError('No x86/amd64 components found; refusing unchanged copy')
    filtered = source
    for first, last in sorted(ranges, reverse=True):
        filtered = filtered[:first] + filtered[last:]

    before, after = ET.fromstring(source), ET.fromstring(filtered)
    tag = f'{{{UNATTEND}}}component'
    remaining = list(after.iter(tag))
    if any(c.get('processorArchitecture', 'arm64').lower() != 'arm64' for c in remaining):
        raise ValueError('Filtered answer file still has non-ARM64 components')
    def content(element):
        element = deepcopy(element)
        # Whitespace between components grows when an intervening block is removed.
        element.tail = None
        return ET.tostring(element)

    original_arm = [content(c) for c in before.iter(tag)
                    if c.get('processorArchitecture', 'arm64').lower() == 'arm64']
    if [content(c) for c in remaining] != original_arm:
        raise ValueError('ARM64 settings changed')
    original_extensions = [ET.tostring(c) for c in before if c.tag.endswith('}Extensions')]
    if [ET.tostring(c) for c in after if c.tag.endswith('}Extensions')] != original_extensions:
        raise ValueError('Embedded scripts changed')
    return filtered, len(ranges)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()

    result, count = filter_answer(Path(args.source).read_bytes())
    with Path(args.target).open('xb') as output:
        output.write(result)
    print(f'Removed {count} x86/amd64 components; all remaining bytes preserved')


if __name__ == "__main__":
    main()

"""Parse CUBRID core backtraces and match them against signatures."""

import hashlib
import re

# gdb detail stack: "#8  0x00007f... in pgbuf_fix_debug (" or "#10 oos_read (thread_p=...)"
_GDB_FRAME = re.compile(r"^#(\d+)\s{1,3}(?:0x[0-9a-fA-F]+ in )?([^\s(<][^\s(]*) \(")
# CTP summary stack: "#8\tpgbuf_fix_debug\tsrc/storage/page_buffer.c"
_CTP_FRAME = re.compile(r"^#(\d+)\t([^\t]+)\t")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse_stacks(text: str) -> list[list[str]]:
    """Return one frame list per stack, innermost first.

    A frame numbered 0 starts a new stack, so a CTP log that prints a core twice (gdb detail
    and summary) or holds several cores yields separate stacks. The gdb form is preferred
    because CTP prints the same core in both forms.
    """
    lines = text.splitlines()
    for pattern in (_GDB_FRAME, _CTP_FRAME):
        stacks: list[list[str]] = []
        for line in lines:
            m = pattern.match(line)
            if not m:
                continue
            if m.group(1) == "0" or not stacks:
                stacks.append([])
            stacks[-1].append(m.group(2))
        if stacks:
            return stacks
    return []


def is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(frame == want for frame in it) for want in needle)


def matches(text: str, frames: list[str], required_text: list[str]) -> bool:
    """A frames signature needs its frames in a parsed stack. A text-only one has no frames."""
    if not all(t in text for t in required_text):
        return False
    return not frames or any(is_subsequence(frames, stack) for stack in parse_stacks(text))

"""The guardian family block is the same, byte for byte, in all four repos.

Each repo carries this test with the same hash, so an edit to the block in one
README fails here until it is made in all four.
"""
import hashlib
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
START = "<!-- guardian-family:start -->"
END = "<!-- guardian-family:end -->"
SHA256 = "0cd2ecb7fd53801c8aacae8e4ed48d9712dd97aba8503aca9ffcf0e48d7f8430"


def _block():
    lines = README.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln == START]
    ends = [i for i, ln in enumerate(lines) if ln == END]
    assert len(starts) == 1 and len(ends) == 1, "README needs exactly one %s ... %s block" % (START, END)
    assert starts[0] < ends[0]
    return "\n".join(lines[starts[0]:ends[0] + 1]).rstrip("\n")


def test_family_block_matches_the_shared_hash():
    got = hashlib.sha256(_block().encode("utf-8")).hexdigest()
    assert got == SHA256, "the guardian family block differs from the one shared by all four repos"

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Which CPython interpreter a WoA wheel installs on, decided by its tags as pip decides it.

Note [Compatibility is pip's, not a list of tag shapes]
    Upstream picks the tags of the torchaudio and torchvision wheels, and has
    changed them without notice: one wheel per Python (`cp313-cp313`), one
    stable-ABI wheel (`cp310-abi3`), and since 2026-10-07 one `py3-none` wheel
    for every Python (pytorch/audio#4234, pytorch/vision#9643). A check written
    against the shapes seen so far refuses the next one. So a wheel belongs with
    an interpreter exactly when one of its tags is among the tags that
    interpreter supports, listed here the way `packaging.tags.sys_tags()` lists
    them for CPython; `tests/test_wheel_tags.py` holds the two to the same set.
    That includes the free-threaded stable ABI, `abi3t` (PEP 803, packaging
    26.3), which a free-threaded interpreter takes in place of `abi3`.
    This stays standard-library only, like the rest of `scripts/publish`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Tag = tuple[str, str, str]

_CPYTHON = re.compile(r"^cp3(\d+)$")


def tags_of(filename: str) -> frozenset[Tag]:
    """The `(python, abi, platform)` tags a wheel filename declares, compressed sets expanded."""
    parts = filename[:-len(".whl")].split("-") if filename.endswith(".whl") else []
    if len(parts) not in (5, 6):
        raise ValueError(f"not a wheel filename: {filename!r}")
    pythons, abis, platforms = (field.split(".") for field in parts[-3:])
    return frozenset((p, a, x) for p in pythons for a in abis for x in platforms)


@dataclass(frozen=True)
class Interpreter:
    """One CPython 3 interpreter on one platform."""

    minor: int
    free_threaded: bool
    platform: str

    def __str__(self) -> str:
        return f"cp3{self.minor}-cp3{self.minor}{'t' if self.free_threaded else ''}-{self.platform}"

    def supported_tags(self) -> frozenset[Tag]:
        python = f"cp3{self.minor}"
        tags = {(python, python + ("t" if self.free_threaded else ""), self.platform), (python, "none", self.platform)}
        stable_abi = "abi3t" if self.free_threaded else "abi3"
        tags |= {(f"cp3{minor}", stable_abi, self.platform) for minor in range(2, self.minor + 1)}
        generic = ["py3"] + [f"py3{minor}" for minor in range(self.minor + 1)]
        tags |= {(py, "none", platform) for py in generic for platform in (self.platform, "any")}
        tags.add((python, "none", "any"))
        return frozenset(tags)

    def installs(self, filename: str) -> bool:
        """Whether pip on this interpreter would install `filename`. See
        Note [Compatibility is pip's, not a list of tag shapes]."""
        return not tags_of(filename).isdisjoint(self.supported_tags())


def interpreter_of(filename: str) -> Interpreter:
    """The interpreter a wheel built for exactly one of them, as torch's is, targets."""
    tags = tags_of(filename)
    if len(tags) == 1:
        ((python, abi, platform),) = tags
        match = _CPYTHON.match(python)
        if match and abi in (python, python + "t"):
            return Interpreter(int(match.group(1)), abi.endswith("t"), platform)
    raise ValueError(f"{filename} is not built for exactly one CPython interpreter")

"""Download the MTR source tree at a fixed commit, check the archive's SHA-256 and unpack it.

    python fetch_mtr.py <commit> <sha256 of the archive> <target directory>

Used by Dockerfile.open-nnde. MTR: https://github.com/sshaoshuai/MTR (Apache-2.0).
"""

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path


def main(commit: str, sha256: str, target: str) -> None:
    url = f"https://codeload.github.com/sshaoshuai/MTR/tar.gz/{commit}"
    with urllib.request.urlopen(url, timeout=300) as reply:
        data = reply.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        sys.exit(f"MTR archive {commit}: sha256 {digest}, expected {sha256}")
    prefix = f"MTR-{commit}/"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for member in tar.getmembers():
            if not member.name.startswith(prefix) or not (member.isfile() or member.isdir()):
                continue
            member.name = member.name[len(prefix) :]
            if member.name:
                members.append(member)
        tar.extractall(target, members=members, filter="data")
    print(f"MTR {commit} -> {Path(target)}")


if __name__ == "__main__":
    main(*sys.argv[1:4])

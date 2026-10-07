#!/usr/bin/env python3
from pathlib import Path
import hashlib
import sys
import zipfile


def main(argv: list[str] | None = None) -> int:
    args = sys.argv if argv is None else argv
    if len(args) != 3:
        print(f"usage: {Path(args[0]).name} <seed.zip> <boot_seed.img>")
        return 2

    src = Path(args[1])
    dst = Path(args[2])

    with zipfile.ZipFile(src, "r") as z:
        bad = z.testzip()
        if bad:
            print(f"SEED_ZIP_BAD_ENTRY={bad}")
            return 3
        try:
            data = z.read("seeds/boot_seed.img")
        except KeyError:
            print("BOOT_SEED_MEMBER_MISSING=1")
            return 4

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)

    print(f"BOOT_SEED_EXTRACTED={dst}")
    print(f"BOOT_SEED_SIZE={len(data)}")
    print(f"BOOT_SEED_SHA256={hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

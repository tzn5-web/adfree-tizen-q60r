#!/usr/bin/env python3
from pathlib import Path
import hashlib
import sys
import zipfile

if len(sys.argv) != 3:
    print(f"usage: {Path(sys.argv[0]).name} <seed.zip> <boot_seed.img>")
    raise SystemExit(2)

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

with zipfile.ZipFile(src, "r") as z:
    bad = z.testzip()
    if bad:
        print(f"SEED_ZIP_BAD_ENTRY={bad}")
        raise SystemExit(3)

    try:
        data = z.read("seeds/boot_seed.img")
    except KeyError:
        print("BOOT_SEED_MEMBER_MISSING=1")
        raise SystemExit(4)

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_bytes(data)

print(f"BOOT_SEED_EXTRACTED={dst}")
print(f"BOOT_SEED_SIZE={len(data)}")
print(f"BOOT_SEED_SHA256={hashlib.sha256(data).hexdigest()}")
raise SystemExit(0)

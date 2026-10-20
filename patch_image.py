# -*- coding: utf-8 -*-
"""Patch an Armbian Rock Pi 4A image for the Signway AI3399-C.

The Armbian board id stays rockpi-4a so the existing boot script and
fdtfile=rockchip/rk3399-rock-pi-4a.dtb keep working. Only the visible
model string, hostname and BOARD_NAME change. /etc/rc.local installs
and starts the MCU watchdog, so a later flash needs no manual step.

Usage:
  python patch_image.py STOCK.img --out ai3399-c-armbian.img
  python patch_image.py ai3399-c-armbian.img --check
"""
import argparse
import shutil
import struct
import sys
from pathlib import Path

FS_OFF = 32768 * 512
OLD_PLUS = b"Radxa ROCK Pi 4A+"
NEW_PLUS = b"Signway AI3399-C\x00"
OLD = b"Radxa ROCK Pi 4A"
NEW = b"Signway AI3399-C"
ROOT = Path(__file__).resolve().parent
RC_LOCAL_PATH = ROOT / "rootfs" / "etc" / "rc.local"

def crc32c_le(crc, data):
    tab = getattr(crc32c_le, "tab", None)
    if tab is None:
        poly = 0x82F63B78
        tab = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ poly if c & 1 else c >> 1
            tab.append(c)
        crc32c_le.tab = tab
    for b in data:
        crc = tab[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF

class Ext4:
    def __init__(self, path, write=False):
        self.f = open(path, "r+b" if write else "rb")
        self.fs = FS_OFF
        self.f.seek(self.fs + 1024)
        self.sb = bytearray(self.f.read(1024))
        if struct.unpack_from("<H", self.sb, 0x38)[0] != 0xEF53:
            raise SystemExit("not an ext4 superblock at the Armbian offset")
        self.block_size = 1024 << struct.unpack_from("<I", self.sb, 0x18)[0]
        self.inode_size = struct.unpack_from("<H", self.sb, 0x58)[0]
        self.inodes_per_group = struct.unpack_from("<I", self.sb, 0x28)[0]
        self.blocks_per_group = struct.unpack_from("<I", self.sb, 0x20)[0]
        self.blocks_count = struct.unpack_from("<I", self.sb, 4)[0] | (struct.unpack_from("<I", self.sb, 0x150)[0] << 32)
        self.inodes_count = struct.unpack_from("<I", self.sb, 0x10)[0]
        self.desc_size = struct.unpack_from("<H", self.sb, 0xFE)[0] or 32
        self.seed = struct.unpack_from("<I", self.sb, 0x270)[0]
        self.ngroups = (self.blocks_count + self.blocks_per_group - 1) // self.blocks_per_group
        self.inode_table = []
        self.f.seek(self.fs + self.block_size)
        raw = self.f.read(self.ngroups * self.desc_size)
        for i in range(self.ngroups):
            gd = raw[i * self.desc_size:(i + 1) * self.desc_size]
            lo = struct.unpack_from("<I", gd, 8)[0]
            hi = struct.unpack_from("<I", gd, 0x28)[0] if self.desc_size >= 0x2C else 0
            self.inode_table.append((hi << 32) | lo)

    def super_ok(self):
        calc = crc32c_le(0xFFFFFFFF, bytes(self.sb[:0x3FC]))
        stored = struct.unpack_from("<I", self.sb, 0x3FC)[0]
        return calc == stored

    def read_inode(self, ino):
        idx = ino - 1
        grp = idx // self.inodes_per_group
        off = idx % self.inodes_per_group
        pos = self.fs + self.inode_table[grp] * self.block_size + off * self.inode_size
        self.f.seek(pos)
        return bytearray(self.f.read(self.inode_size))

    def write_inode(self, ino, raw):
        idx = ino - 1
        grp = idx // self.inodes_per_group
        off = idx % self.inodes_per_group
        pos = self.fs + self.inode_table[grp] * self.block_size + off * self.inode_size
        self.f.seek(pos)
        self.f.write(raw)

    def inode_csum(self, ino, raw):
        tmp = bytearray(raw)
        extra = struct.unpack_from("<H", tmp, 128)[0]
        if extra < 4:
            raise SystemExit("inode %d has no checksum_hi" % ino)
        struct.pack_into("<H", tmp, 124, 0)
        struct.pack_into("<H", tmp, 130, 0)
        crc = crc32c_le(self.seed, struct.pack("<I", ino))
        crc = crc32c_le(crc, bytes(tmp[100:104]))
        crc = crc32c_le(crc, bytes(tmp))
        return crc

    def stored_csum(self, raw):
        lo = struct.unpack_from("<H", raw, 124)[0]
        hi = struct.unpack_from("<H", raw, 130)[0]
        return lo | (hi << 16)

    def csum_ok(self, ino, raw=None):
        if raw is None:
            raw = self.read_inode(ino)
        return self.inode_csum(ino, raw) == self.stored_csum(raw)

    def extent_leaves(self, raw):
        flags = struct.unpack_from("<I", raw, 32)[0]
        if not flags & 0x80000:
            return []
        return self._parse_node(bytes(raw[40:100]))

    def _parse_node(self, node):
        magic, entries, maxe, depth, gen = struct.unpack_from("<HHHHI", node, 0)
        if magic != 0xF30A:
            raise SystemExit("bad extent magic")
        leaves = []
        if depth == 0:
            for i in range(entries):
                ee_block, ee_len, ee_hi, ee_lo = struct.unpack_from("<IHHI", node, 12 + 12 * i)
                if ee_len & 0x8000:
                    continue
                leaves.append((ee_lo | (ee_hi << 32), ee_len & 0x7FFF))
            return leaves
        for i in range(entries):
            ei_block, ei_lo, ei_hi, unused = struct.unpack_from("<IIHH", node, 12 + 12 * i)
            phys = ei_lo | (ei_hi << 32)
            self.f.seek(self.fs + phys * self.block_size)
            leaves.extend(self._parse_node(self.f.read(self.block_size)))
        return leaves

    def read_file(self, ino):
        raw = self.read_inode(ino)
        size = struct.unpack_from("<I", raw, 4)[0] | (struct.unpack_from("<I", raw, 108)[0] << 32)
        data = bytearray()
        for start, length in self.extent_leaves(raw):
            self.f.seek(self.fs + start * self.block_size)
            data += self.f.read(length * self.block_size)
        return bytes(data[:size]), raw

    def write_file(self, ino, new_data):
        raw = self.read_inode(ino)
        leaves = self.extent_leaves(raw)
        cap = sum(n for _, n in leaves) * self.block_size
        if not leaves or len(new_data) > cap:
            raise SystemExit("inode %d cannot grow past its existing blocks (%d > %d)" % (ino, len(new_data), cap))
        remaining = new_data
        for idx, (start, length) in enumerate(leaves):
            chunk = length * self.block_size
            take = min(len(remaining), chunk)
            piece = remaining[:take]
            buf = piece if take == chunk else piece.ljust(chunk, b"\x00")
            self.f.seek(self.fs + start * self.block_size)
            self.f.write(buf)
            remaining = remaining[take:]
            if not remaining:
                for start2, length2 in leaves[idx + 1:]:
                    self.f.seek(self.fs + start2 * self.block_size)
                    self.f.write(b"\x00" * (length2 * self.block_size))
                break
        if remaining:
            raise SystemExit("short write")
        struct.pack_into("<I", raw, 4, len(new_data) & 0xFFFFFFFF)
        struct.pack_into("<I", raw, 108, (len(new_data) >> 32) & 0xFFFFFFFF)
        csum = self.inode_csum(ino, raw)
        struct.pack_into("<H", raw, 124, csum & 0xFFFF)
        struct.pack_into("<H", raw, 130, (csum >> 16) & 0xFFFF)
        self.write_inode(ino, raw)

    def lookup(self, path):
        ino = 2
        for part in [p for p in path.split("/") if p]:
            data, _ = self.read_file(ino)
            found = None
            p = 0
            nb = part.encode()
            while p + 8 <= len(data):
                dinode, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, p)
                if rec_len < 8:
                    break
                if dinode and data[p + 8:p + 8 + name_len] == nb:
                    found = dinode
                    break
                p += rec_len
            if not found:
                raise SystemExit("missing " + path)
            ino = found
        return ino

def find_all(blob, needle):
    out = []
    start = 0
    while True:
        i = blob.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1

def load_rc_local():
    data = RC_LOCAL_PATH.read_bytes().replace(b"\r\n", b"\n")
    if not data.endswith(b"\n"):
        data += b"\n"
    if len(data) > 4096 or b"\r" in data:
        raise SystemExit("rc.local must be LF-only and fit in one 4K block")
    if b"0x1C, 0" not in data or b"0x20, 1" not in data:
        raise SystemExit("rc.local is missing the watchdog writes")
    return data

def file_block_map(ext):
    files = {}
    journal = set()
    for ino in range(1, ext.inodes_count + 1):
        raw = ext.read_inode(ino)
        mode = struct.unpack_from("<H", raw, 0)[0]
        if (mode & 0xF000) != 0x8000:
            continue
        try:
            leaves = ext.extent_leaves(raw)
        except SystemExit:
            continue
        for start, length in leaves:
            for block in range(start, start + length):
                if ino == 8:
                    journal.add(block)
                else:
                    files.setdefault(block, ino)
    return files, journal

def classify(ext, files, journal, off):
    if off < FS_OFF:
        return "boot", None
    block = (off - FS_OFF) // ext.block_size
    if block in journal:
        return "journal", None
    if block in files:
        return "file", files[block]
    return "meta", None

def text_plan(ext, rc_local):
    edits = []
    host_ino = ext.lookup("/etc/hostname")
    hosts_ino = ext.lookup("/etc/hosts")
    rel_ino = ext.lookup("/etc/armbian-release")
    img_ino = ext.lookup("/etc/armbian-image-release")
    rc_ino = ext.lookup("/etc/rc.local")
    host, _ = ext.read_file(host_ino)
    hosts, _ = ext.read_file(hosts_ino)
    rel, _ = ext.read_file(rel_ino)
    imgrel, _ = ext.read_file(img_ino)
    rc, _ = ext.read_file(rc_ino)
    if b"BOARD=rockpi-4a" not in rel or b"BOARD=rockpi-4a" not in imgrel:
        raise SystemExit("refusing: BOARD=rockpi-4a is missing; this is not the expected image")
    if host == b"rockpi-4a\n":
        edits.append((host_ino, "/etc/hostname", b"ai3399-c\n"))
    elif host != b"ai3399-c\n":
        raise SystemExit("unexpected /etc/hostname: %r" % host)
    if b"rockpi-4a" in hosts:
        edits.append((hosts_ino, "/etc/hosts", hosts.replace(b"rockpi-4a", b"ai3399-c")))
    elif b"ai3399-c" not in hosts:
        raise SystemExit("unexpected /etc/hosts")
    old_name = b'BOARD_NAME="Rockpi 4A"'
    new_name = b'BOARD_NAME="Signway AI3399-C"'
    for ino, path, blob in ((rel_ino, "/etc/armbian-release", rel), (img_ino, "/etc/armbian-image-release", imgrel)):
        if old_name in blob:
            edits.append((ino, path, blob.replace(old_name, new_name)))
        elif new_name not in blob:
            raise SystemExit("unexpected BOARD_NAME in " + path)
    if rc != rc_local:
        edits.append((rc_ino, "/etc/rc.local", rc_local))
    return edits

def patch(path, check_only):
    rc_local = load_rc_local()
    ext = Ext4(path, write=False)
    if not ext.super_ok():
        raise SystemExit("superblock checksum mismatch")
    state = struct.unpack_from("<H", ext.sb, 0x3A)[0]
    incompat = struct.unpack_from("<I", ext.sb, 0x60)[0]
    if state != 1 or (incompat & 4):
        raise SystemExit("filesystem is not clean; unmount it before patching")
    for ino in (2, 721, 722, 744, 745, 774):
        if not ext.csum_ok(ino):
            raise SystemExit("inode %d checksum mismatch before edit" % ino)
    print("scanning file blocks")
    files, journal = file_block_map(ext)
    blob = Path(path).read_bytes()
    plus = find_all(blob, OLD_PLUS)
    plain = [i for i in find_all(blob, OLD) if i not in set(plus)]
    plan = []
    for off in plus:
        kind, ino = classify(ext, files, journal, off)
        plan.append((off, OLD_PLUS, NEW_PLUS, kind, ino))
    for off in plain:
        kind, ino = classify(ext, files, journal, off)
        plan.append((off, OLD, NEW, kind, ino))
    bad = [item for item in plan if item[3] not in ("boot", "file")]
    for off, old, new, kind, ino in plan:
        print("  model off=%d %s ino=%s" % (off, kind, ino))
    if bad:
        raise SystemExit("refusing to patch %d unexpected model-string locations" % len(bad))
    edits = text_plan(ext, rc_local)
    for ino, name, data in edits:
        print("  edit %s ino=%d -> %d bytes" % (name, ino, len(data)))
    if b"rk3399-rock-pi-4a.dtb" not in ext.read_file(ext.lookup("/boot/armbianEnv.txt"))[0]:
        raise SystemExit("fdtfile is missing")
    ext.f.close()
    if check_only:
        print("check ok; %d model strings, %d file edits" % (len(plan), len(edits)))
        return
    if not plan and not edits:
        print("already patched")
        return
    ext = Ext4(path, write=True)
    with ext.f:
        for off, old, new, kind, ino in plan:
            ext.f.seek(off)
            cur = ext.f.read(len(old))
            if cur != old:
                raise SystemExit("offset %d changed" % off)
            ext.f.seek(off)
            ext.f.write(new)
        for ino, name, data in edits:
            ext.write_file(ino, data)
            if not ext.csum_ok(ino):
                raise SystemExit("checksum failed after writing " + name)
        ext.f.flush()
    ext = Ext4(path, write=False)
    if not ext.super_ok():
        raise SystemExit("superblock checksum changed")
    blob = Path(path).read_bytes()
    if OLD in blob or OLD_PLUS in blob or b'BOARD_NAME="Rockpi 4A"' in blob:
        raise SystemExit("old identity remains")
    rel, raw = ext.read_file(ext.lookup("/etc/armbian-release"))
    if b"BOARD=rockpi-4a" not in rel or b'Signway AI3399-C' not in rel:
        raise SystemExit("release file is wrong")
    if not ext.csum_ok(ext.lookup("/etc/rc.local")):
        raise SystemExit("rc.local checksum failed")
    dtb, _ = ext.read_file(ext.lookup("/boot/dtb-6.18.57-current-rockchip64/rockchip/rk3399-rock-pi-4a.dtb"))
    if dtb[:4] != b"\xd0\x0d\xfe\xed" or b"Signway AI3399-C" not in dtb:
        raise SystemExit("kernel dtb was not patched")
    ext.f.close()
    print("patched", path)

def main():
    parser = argparse.ArgumentParser(description="Bake the AI3399-C watchdog and board name into an Armbian image")
    parser.add_argument("image", type=Path, help="stock or already patched Armbian image")
    parser.add_argument("--out", type=Path, help="write a copy, leaving the source image unchanged")
    parser.add_argument("--check", action="store_true", help="verify and print the plan without writing")
    args = parser.parse_args()
    if not args.image.is_file():
        raise SystemExit("no such image: %s" % args.image)
    target = args.image
    if args.out and args.out.resolve() != args.image.resolve():
        if args.check:
            target = args.image
        else:
            print("copying to", args.out)
            shutil.copyfile(args.image, args.out)
            target = args.out
    patch(target, args.check)

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)

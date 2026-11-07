#!/usr/bin/env python3
"""
Repair recovered DJI MP4s by finding each one's 'moov' index ANYWHERE on the
memory card and matching it by exact size.

Each index lists the exact size of every video/audio frame. A recovered file
gets an index only if those sizes add up exactly to the file's video data, so
an index from a different video can't be attached by mistake.

Reads the memory card READ-ONLY. Never changes recup.* files.
Repaired copies go to ~/Recovered-USB/fixed2/

Usage:  sudo python3 -I ~/Recovered-USB/fix_moov2.py [/dev/rdisk2]
"""
import glob, os, struct, subprocess, sys, time, shutil

DEV = sys.argv[1] if len(sys.argv) > 1 else "/dev/rdisk2"
HOME = os.path.expanduser("~" + os.environ.get("SUDO_USER", ""))
BASE = os.environ.get("RECUP_BASE") or os.path.join(HOME, "Recovered-USB")
OUT = os.path.join(BASE, "fixed2")
CHUNK = 8 << 20
MAX_MOOV = 64 << 20
SECTOR = 512
FFPROBE = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"

def boxes(d, s, e):
    p = s
    while p + 8 <= e:
        n, t = struct.unpack(">I4s", d[p:p + 8]); h = 8
        if n == 1:
            if p + 16 > e: return
            n = struct.unpack(">Q", d[p + 8:p + 16])[0]; h = 16
        if n < 8 or p + n > e: return
        yield t, p + h, p + n
        p += n

def index_info(moov):
    """Total bytes of all samples, and lowest/highest chunk offset."""
    acc = {"sz": 0, "lo": 1 << 62, "hi": 0, "n": 0}
    def walk(s, e):
        for t, a, b in boxes(moov, s, e):
            if t in (b"trak", b"mdia", b"minf", b"stbl"):
                walk(a, b)
            elif t == b"stsz":
                ss, cnt = struct.unpack(">II", moov[a + 4:a + 12])
                acc["sz"] += ss * cnt if ss else sum(struct.unpack(">%dI" % cnt, moov[a + 12:a + 12 + 4 * cnt]))
                acc["n"] += 1
            elif t in (b"stco", b"co64"):
                cnt = struct.unpack(">I", moov[a + 4:a + 8])[0]
                w = 4 if t == b"stco" else 8
                v = struct.unpack(">%d%s" % (cnt, "I" if w == 4 else "Q"), moov[a + 8:a + 8 + cnt * w])
                if v:
                    acc["lo"] = min(acc["lo"], min(v)); acc["hi"] = max(acc["hi"], max(v))
    walk(8, len(moov))
    return acc

# 1. Recovered files with no index: remember their video-data size.
want = {}
for p in glob.glob(os.path.join(BASE, "recup.*", "*.mp4")):
    with open(p, "rb") as f: d = f.read(1 << 16)
    size = os.path.getsize(p)
    pos, md, has_moov = 0, None, False
    with open(p, "rb") as f:
        while pos < size:
            f.seek(pos); h = f.read(16)
            if len(h) < 8: break
            n, t = struct.unpack(">I4s", h[:8]); hl = 8
            if n == 1: n = struct.unpack(">Q", h[8:16])[0]; hl = 16
            if n < 8: break
            if t == b"mdat": md = (pos + hl, pos + n)
            if t == b"moov": has_moov = True
            pos += n
    if md and not has_moov:
        want.setdefault(md[1] - md[0], []).append((p, md))
print(f"{sum(len(v) for v in want.values())} videos need an index. Scanning {DEV} (read-only)...")

# 2. Scan the whole drive for index blocks.
os.makedirs(OUT, exist_ok=True)
fd = os.open(DEV, os.O_RDONLY)
def read_at(off, n):
    start = off - off % SECTOR
    end = off + n; end += (-end) % SECTOR
    os.lseek(fd, start, os.SEEK_SET)
    buf = bytearray()
    while len(buf) < end - start:
        c = os.read(fd, min(CHUNK, end - start - len(buf)))
        if not c: break
        buf += c
    return bytes(buf[off - start: off - start + n])

done, found_idx, t0 = set(), 0, time.time()
off, prev_tail = 0, b""
while True:
    os.lseek(fd, off, os.SEEK_SET)
    try:
        buf = os.read(fd, CHUNK)
    except OSError:
        buf = b""
    if not buf: break
    data = prev_tail + buf
    base = off - len(prev_tail)
    i = data.find(b"moov")
    while i != -1:
        if i >= 4 and b"mvhd" in data[i + 4:i + 40]:
            n = struct.unpack(">I", data[i - 4:i])[0]
            if 16 < n <= MAX_MOOV:
                moov = read_at(base + i - 4, n)
                try:
                    info = index_info(moov)
                except Exception:
                    info = None
                if info and info["n"]:
                    found_idx += 1
                    for p, md in want.get(info["sz"], []):
                        if p in done: continue
                        if not (md[0] <= info["lo"] and info["hi"] < md[1]): continue
                        dst = os.path.join(OUT, os.path.basename(p))
                        with open(p, "rb") as src, open(dst, "wb") as o:
                            o.write(src.read(md[1])); o.write(moov)
                        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                                            "-of", "csv=p=0", dst], capture_output=True, text=True)
                        print(f"OK {os.path.basename(p)}: {md[1] >> 20} MB, {r.stdout.strip() or '?'} s")
                        done.add(p)
                        break
        i = data.find(b"moov", i + 1)
    prev_tail = data[-64:]
    off += len(buf)
    if off % (4 << 30) < CHUNK:
        el = time.time() - t0
        print(f"   ...scanned {off >> 30} GB ({el / 60:.0f} min), repaired {len(done)} so far")
os.close(fd)

user = os.environ.get("SUDO_USER")
if user: subprocess.run(["chown", "-R", user, OUT])
total = sum(len(v) for v in want.values())
print(f"\nFound {found_idx} index blocks on the drive.")
print(f"Repaired {len(done)} of {total} videos. Copies are in {OUT}")

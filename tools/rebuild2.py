#!/usr/bin/env python3
"""
Rebuild shuffled DJI videos by matching every frame on the drive.

1. From each video's index (fixed2/), list every frame: its exact size and
   where inside a 128 KB piece it must start.
2. Read the whole memory card ONCE (read-only). Wherever a frame header appears
   whose size AND position-inside-the-piece match a known frame, that tells
   us exactly which piece of which video sits there.
3. Pieces with no frame start inside them (middle of a big frame) are placed
   next to their neighbours.

Never writes to the memory card. Never changes recup.* or fixed2/.
Rebuilt videos go to ~/Recovered-USB/rebuilt2/

Usage:  sudo python3 -I ~/Recovered-USB/rebuild2.py [/dev/rdisk2]
"""
import glob, os, pickle, re, shutil, struct, subprocess, sys, time

DEV = sys.argv[1] if len(sys.argv) > 1 else "/dev/rdisk2"
HOME = os.path.expanduser("~" + os.environ.get("SUDO_USER", ""))
BASE = os.environ.get("RECUP_BASE") or os.path.join(HOME, "Recovered-USB")
SRC = os.path.join(BASE, "fixed2")
OUT = os.path.join(BASE, "rebuilt2")
SCAN_CACHE = os.path.join(BASE, ".drive_scan.pickle")
PART_OFFSET = int(os.environ.get("PART_OFFSET", 64 * 512))
C = 128 * 1024
READ = 32 << 20
FFPROBE = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"
FFMPEG = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"

# ------------------------------------------------------------------ mp4 index
def boxes(d, s, e):
    p = s
    while p + 8 <= e:
        n, t = struct.unpack(">I4s", d[p:p + 8]); h = 8
        if n == 1: n = struct.unpack(">Q", d[p + 8:p + 16])[0]; h = 16
        if n < 8 or p + n > e: return
        yield t, p + h, p + n
        p += n

def child(d, s, e, name):
    for t, a, b in boxes(d, s, e):
        if t == name: return a, b

def video_samples(moov):
    """[(offset, size, kind)] for video samples."""
    out = []
    for t, a, b in boxes(moov, 8, len(moov)):
        if t != b"trak": continue
        m = child(moov, a, b, b"mdia"); m = m and child(moov, *m, b"minf"); st = m and child(moov, *m, b"stbl")
        if not st: continue
        tabs = {tt: (aa, bb) for tt, aa, bb in boxes(moov, *st)}
        if not all(k in tabs for k in (b"stsd", b"stsz", b"stsc")): continue
        fmt = moov[tabs[b"stsd"][0] + 12:tabs[b"stsd"][0] + 16]
        kind = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc"}.get(fmt)
        if not kind: continue
        q = tabs[b"stsz"][0]; ss, cnt = struct.unpack(">II", moov[q + 4:q + 12])
        sizes = [ss] * cnt if ss else struct.unpack(">%dI" % cnt, moov[q + 12:q + 12 + 4 * cnt])
        if b"stco" in tabs:
            q = tabs[b"stco"][0]; n = struct.unpack(">I", moov[q + 4:q + 8])[0]
            chunks = struct.unpack(">%dI" % n, moov[q + 8:q + 8 + 4 * n])
        elif b"co64" in tabs:
            q = tabs[b"co64"][0]; n = struct.unpack(">I", moov[q + 4:q + 8])[0]
            chunks = struct.unpack(">%dQ" % n, moov[q + 8:q + 8 + 8 * n])
        else: continue
        q = tabs[b"stsc"][0]; n = struct.unpack(">I", moov[q + 4:q + 8])[0]
        stsc = [struct.unpack(">III", moov[q + 8 + 12 * i:q + 20 + 12 * i]) for i in range(n)]
        si = 0
        for ci, co in enumerate(chunks, 1):
            per = 0
            for first, spc, _ in stsc:
                if first <= ci: per = spc
            o = co
            for _ in range(per):
                if si >= len(sizes): break
                out.append((o, sizes[si], kind)); o += sizes[si]; si += 1
    return sorted(out)

def load_videos():
    vids = []
    for p in glob.glob(os.path.join(SRC, "*.mp4")):
        d = open(p, "rb").read()
        md = mv = None
        for t, a, b in boxes(d, 0, len(d)):
            if t == b"mdat": md = (a, b)
            if t == b"moov": mv = (a - 8, b)
        if not (md and mv): continue
        moov = d[mv[0]:mv[1]]
        vids.append({"path": p, "name": os.path.basename(p), "total": md[1],
                     "head": d[:C], "moov": moov, "frames": video_samples(moov)})
    return vids

# ------------------------------------------------------------------ drive scan
# A frame starts with a 4-byte length then a NAL header.
HEVC_RE = re.compile(rb"\x00[\x00-\x7f][\x00-\xff]{2}[\x00-\x2b\x40-\x4f]\x01", re.S)
H264_RE = re.compile(rb"\x00[\x00-\x7f][\x00-\xff]{2}[\x01\x05\x06\x09\x21\x25\x41\x45\x61\x65\x67\x68]", re.S)

PHASE = PART_OFFSET % C

def device_size(path, fd):
    """Size in bytes. lseek(SEEK_END) returns 0 for raw disks on macOS, so ask the disk."""
    import stat, fcntl
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return os.fstat(fd).st_size
    try:
        cnt = bytearray(8); bs = bytearray(4)
        fcntl.ioctl(fd, 0x40086419, cnt)       # DKIOCGETBLOCKCOUNT
        fcntl.ioctl(fd, 0x40046418, bs)        # DKIOCGETBLOCKSIZE
        n = int.from_bytes(cnt, sys.byteorder) * int.from_bytes(bs, sys.byteorder)
        if n > 0: return n
    except OSError:
        pass
    import plistlib
    name = "/dev/" + os.path.basename(path).lstrip("r")
    info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", name],
                                         capture_output=True).stdout)
    return int(info.get("TotalSize") or info.get("Size"))

def read_span(fd, off, n):
    """Read n bytes at off; on a read error fall back to 1 MB pieces, skipping bad ones."""
    os.lseek(fd, off, os.SEEK_SET)
    try:
        return os.read(fd, n), False
    except OSError:
        out = bytearray()
        for o in range(off, off + n, 1 << 20):
            try:
                os.lseek(fd, o, os.SEEK_SET); out += os.read(fd, min(1 << 20, off + n - o))
            except OSError:
                out += b"\0" * min(1 << 20, off + n - o)
        return bytes(out), True

def find_starts(vids):
    """Locate each video's first piece on the drive and the drive's cluster phase."""
    global PHASE
    d = Disk(DEV)
    from collections import Counter
    ph = Counter()
    for v in vids:
        sector = int(re.search(r"f(\d+)", v["name"]).group(1))
        v["start"] = None
        for base in (PART_OFFSET, 0):
            if d.read(base + sector * 512, 64) == v["head"][:64]:
                v["start"] = base + sector * 512; ph[v["start"] % C] += 1; break
    if ph: PHASE = ph.most_common(1)[0][0]

def scan_drive(keys_hevc, keys_h264):
    """Return list of (disk_pos, length, kind) for frame headers matching a known (length, pos%C)."""
    if os.path.exists(SCAN_CACHE) and os.environ.get("RESCAN") is None:
        with open(SCAN_CACHE, "rb") as f:
            cached = pickle.load(f)
        if cached.get("dev") == DEV and cached.get("n") == len(keys_hevc) + len(keys_h264):
            print("Using the saved drive scan from last time.")
            return cached["hits"]
    fd = os.open(DEV, os.O_RDONLY)
    size = device_size(DEV, fd)
    print(f"Drive size: {size / 1e9:.1f} GB")
    if size <= 0:
        sys.exit("Could not read the drive size. Nothing was changed.")
    hits, off, tail, t0 = [], 0, b"", time.time()
    bad = 0
    last_report = 0
    while off < size:
        buf, err = read_span(fd, off, min(READ, size - off))
        bad += err
        if not buf:
            off += C; tail = b""; continue
        data = tail + buf; base = off - len(tail)
        # Empty (all-zero) areas hold no video and are very slow to search,
        # so only search the stretches that contain data.
        spans, s0, ZB = [], None, 64 * 1024
        for b0 in range(0, len(data), ZB):
            blk = data[b0:b0 + ZB]
            empty = not blk.strip(b"\0")
            if not empty and s0 is None: s0 = b0
            if empty and s0 is not None: spans.append((s0, b0 + 8)); s0 = None
        if s0 is not None: spans.append((s0, len(data)))
        for a, e in spans:
            a = max(0, a - 8)
            for rx, keys, kind in ((HEVC_RE, keys_hevc, "hevc"), (H264_RE, keys_h264, "h264")):
                for m in rx.finditer(data, a, min(e, len(data))):
                    pos = base + m.start()
                    L = struct.unpack(">I", data[m.start():m.start() + 4])[0]
                    if (L, (pos - PHASE) % C) in keys:
                        hits.append((pos, L, kind))
        tail = data[-8:]
        off += len(buf)
        if off - last_report >= (4 << 30):
            last_report = off
            el = time.time() - t0
            print(f"   ...read {off >> 30} of {size >> 30} GB ({el / 60:.0f} min)", flush=True)
    os.close(fd)
    if bad: print(f"   ({bad} areas had read errors and were skipped)")
    if not hits:
        sys.exit("The drive scan found no matching frames, so nothing was built. Nothing was changed.")
    with open(SCAN_CACHE, "wb") as f:
        pickle.dump({"dev": DEV, "n": len(keys_hevc) + len(keys_h264), "hits": hits}, f)
    return hits

# ------------------------------------------------------------------ assembly
class Disk:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY)
    def read(self, off, n):
        a = off - off % 512; e = off + n; e += (-e) % 512
        os.lseek(self.fd, a, os.SEEK_SET)
        buf = bytearray()
        while len(buf) < e - a:
            try: b = os.read(self.fd, e - a - len(buf))
            except OSError: b = b""
            if not b: break
            buf += b
        return bytes(buf[off - a: off - a + n])

def nal_end_ok(disk, pos, L, kind):
    """Light check that the bytes after a matched header look like the same kind of frame."""
    h = disk.read(pos, 6)
    return len(h) == 6 and struct.unpack(">I", h[:4])[0] == L

def main():
    vids = load_videos()
    if os.environ.get("ONLY"):
        vids = [v for v in vids if os.environ["ONLY"] in v["name"]]
    print(f"Loaded indexes for {len(vids)} videos.")
    # frame catalog: (length, offset-in-piece) -> [(video#, logical cluster)]
    cat = {}
    for vi, v in enumerate(vids):
        for o, s, k in v["frames"]:
            if o + 4 > v["total"]: continue
            # frame's first NAL length = s-4 for single-NAL frames; we key on the first NAL
            # length read later from the drive, so key on every plausible first-NAL length:
            # use exact sample size (single NAL) — the common DJI case.
            cat.setdefault((k, s - 4, o % C), []).append((vi, o // C))
    find_starts(vids)
    keys_hevc = {(L, r) for (k, L, r) in cat if k == "hevc"}
    keys_h264 = {(L, r) for (k, L, r) in cat if k == "h264"}
    print(f"Catalogued {sum(len(x) for x in cat.values())} frames. Reading the whole drive (read-only)...")
    fresh = not os.path.exists(SCAN_CACHE) or os.environ.get("RESCAN") is not None
    hits = scan_drive(keys_hevc, keys_h264)
    if fresh and os.path.isdir(OUT):
        old = glob.glob(os.path.join(OUT, "*.mp4"))
        for f in old: os.remove(f)
        if old: print(f"Removed {len(old)} videos built from an earlier scan.")
    print(f"Found {len(hits)} matching frame headers on the drive.")

    # votes: video -> logical cluster -> {physical cluster start: count}
    votes = [dict() for _ in vids]
    for pos, L, kind in hits:
        r = (pos - PHASE) % C
        phys = pos - r
        for vi, lc in cat.get((kind, L, r), []):
            votes[vi].setdefault(lc, {}).setdefault(phys, 0)
            votes[vi][lc][phys] += 1

    disk = Disk(DEV)
    os.makedirs(OUT, exist_ok=True)
    claimed = {}
    # first pass: claim clusters that have exactly one candidate
    for vi in range(len(vids)):
        for lc, cands in votes[vi].items():
            if len(cands) == 1:
                ph = next(iter(cands))
                claimed.setdefault(ph, set()).add(vi)

    order = sorted(range(len(vids)), key=lambda i: -vids[i]["total"])
    stats = {"clean": 0, "partial": 0}
    t0 = time.time()
    for n, vi in enumerate(order, 1):
        v = vids[vi]
        final_glob = glob.glob(os.path.join(OUT, "*_" + v["name"]))
        if final_glob: continue
        if shutil.disk_usage(OUT).free < v["total"] + (3 << 30):
            print("\nStopping: your Mac is nearly out of space. Free some and run again "
                  "(the drive scan is saved, so it continues quickly)."); break
        ncl = (v["total"] + C - 1) // C
        mapping = {}
        if v["start"] is not None: mapping[0] = v["start"]
        for lc, cands in votes[vi].items():
            if lc == 0 and 0 in mapping: continue
            # prefer candidates not claimed by another video, then most votes
            best = sorted(cands.items(), key=lambda kv: (len(claimed.get(kv[0], {vi}) - {vi}) > 0, -kv[1]))
            mapping[lc] = best[0][0]
        verified = set(mapping)
        # fill clusters with no frame start from neighbours
        changed = True
        while changed:
            changed = False
            for lc in range(ncl):
                if lc in mapping: continue
                prev, nxt = mapping.get(lc - 1), mapping.get(lc + 1)
                if prev is not None and nxt is not None and nxt == prev + 2 * C:
                    mapping[lc] = prev + C; changed = True
        # Remaining gaps (pieces wholly inside a big frame, next to a jump on
        # the drive): extend from the nearest confirmed neighbour, but never
        # onto a piece another video is confirmed to own.
        anchors = sorted(verified)
        import bisect
        for lc in range(ncl):
            if lc in mapping: continue
            i = bisect.bisect_left(anchors, lc)
            j = anchors[i - 1] if i > 0 else None
            k = anchors[i] if i < len(anchors) else None
            opts = [a for a in (j, k) if a is not None]
            opts.sort(key=lambda a: abs(a - lc))
            for a in opts:
                ph = mapping[a] + (lc - a) * C
                if ph not in claimed or claimed[ph] == {vi}:
                    mapping[lc] = ph; break
        tmp = os.path.join(OUT, "_building.mp4")
        missing = 0
        with open(tmp, "wb") as f:
            for lc in range(ncl):
                ln = min(C, v["total"] - lc * C)
                if lc in mapping: f.write(disk.read(mapping[lc], ln))
                else: f.write(b"\0" * ln); missing += 1
            f.write(v["moov"])
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                            "format=duration:format_tags=creation_time:stream=height", "-of",
                            "default=nw=1", tmp], capture_output=True, text=True).stdout
        dur = float((re.search(r"duration=([\d.]+)", r) or [0, "0"])[1])
        ct = (re.search(r"creation_time=(\S+)", r) or [0, ""])[1]
        h = max([int(x) for x in re.findall(r"height=(\d+)", r)] or [0])
        when = ct[:19].replace("T", "_").replace(":", "") if ct else "unknown-date"
        e = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", tmp, "-map", "0:v:0", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        errs = sum(1 for l in e.splitlines() if "aac" not in l.lower())
        nframes = len(v["frames"])
        kind = "preview" if h and h <= 720 else "full"
        final = os.path.join(OUT, f"{when}_{dur:.0f}s_{h}p_{kind}_{v['name']}")
        os.replace(tmp, final)
        found = len(verified)
        pct = 100 * len(mapping) // max(ncl, 1)
        conf = 100 * found // max(ncl, 1)
        good = pct == 100 and errs == 0
        stats["clean" if good else "partial"] += 1
        print(f"[{n}/{len(vids)}] {os.path.basename(final)} -> {pct}% placed "
              f"({conf}% confirmed by exact frame match)"
              + ("" if errs == 0 else f", {errs} decoder errors"), flush=True)
    user = os.environ.get("SUDO_USER")
    if user:
        subprocess.run(["chown", "-R", user, OUT])
        if os.path.exists(SCAN_CACHE): subprocess.run(["chown", user, SCAN_CACHE])
    print(f"\nDone in {(time.time() - t0) / 60:.0f} min. Fully placed: {stats['clean']}, incomplete: {stats['partial']}")
    print(f"Videos are in {OUT}")

if __name__ == "__main__":
    main()

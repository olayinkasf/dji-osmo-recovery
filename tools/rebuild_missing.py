#!/usr/bin/env python3
"""
Recover full-quality videos whose START was lost (so PhotoRec never found them).

1. Read the whole memory card once (read-only). Collect every video index
   ('moov') on it, and every place that looks like the start of a frame.
2. Skip indexes for videos already rebuilt (those in fixed2/).
3. For each remaining index, match its frames (exact size + position inside a
   128 KB piece) to the drive, and put the pieces back in order.
4. The lost file header is re-created from a sibling video of the same camera.

Never writes to the memory card. Never changes recup.*, fixed2/ or rebuilt2/.
Output goes to ~/Recovered-USB/rebuilt_missing/

Usage:  sudo python3 -I ~/Recovered-USB/rebuild_missing.py [/dev/rdisk2]
"""
import array, bisect, glob, hashlib, importlib.util, os, pickle, re, shutil, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rb", os.path.join(HERE, "rebuild2.py"))
rb = importlib.util.module_from_spec(spec); spec.loader.exec_module(rb)

DEV, BASE, C = rb.DEV, rb.BASE, rb.C
OUT = os.path.join(BASE, "rebuilt_missing")
CACHE = os.path.join(BASE, ".drive_scan_missing.pickle")
READ = 32 << 20
MAX_MOOV = 64 << 20

def all_samples(moov):
    """[(offset, size, kind)] for every sample of every track (kind None = not video)."""
    out = []
    for t, a, b in rb.boxes(moov, 8, len(moov)):
        if t != b"trak": continue
        m = rb.child(moov, a, b, b"mdia"); m = m and rb.child(moov, *m, b"minf"); st = m and rb.child(moov, *m, b"stbl")
        if not st: continue
        tabs = {tt: (aa, bb) for tt, aa, bb in rb.boxes(moov, *st)}
        if not all(k in tabs for k in (b"stsd", b"stsz", b"stsc")): continue
        fmt = moov[tabs[b"stsd"][0] + 12:tabs[b"stsd"][0] + 16]
        kind = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc"}.get(fmt)
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

# ------------------------------------------------------------------ one drive pass
def scan():
    if os.path.exists(CACHE) and os.environ.get("RESCAN") is None:
        with open(CACHE, "rb") as f: d = pickle.load(f)
        if d.get("dev") == DEV:
            print("Using the saved drive scan from last time.")
            return d
    fd = os.open(DEV, os.O_RDONLY)
    size = rb.device_size(DEV, fd)
    print(f"Drive size: {size / 1e9:.1f} GB")
    if size <= 0: sys.exit("Could not read the drive size. Nothing was changed.")
    moovs = {}                                 # sha1 -> (disk pos, bytes)
    hpos, hlen = array.array("q"), array.array("I")   # HEVC frame-start candidates
    apos, alen = array.array("q"), array.array("I")   # H.264 frame-start candidates
    disk = rb.Disk(DEV)
    off, tail, t0, last = 0, b"", time.time(), 0
    while off < size:
        buf, _ = rb.read_span(fd, off, min(READ, size - off))
        if not buf: off += C; tail = b""; continue
        data = tail + buf; base = off - len(tail)
        spans, s0, ZB = [], None, 64 * 1024
        for b0 in range(0, len(data), ZB):
            empty = not data[b0:b0 + ZB].strip(b"\0")
            if not empty and s0 is None: s0 = b0
            if empty and s0 is not None: spans.append((s0, b0 + 8)); s0 = None
        if s0 is not None: spans.append((s0, len(data)))
        for a, e in spans:
            a = max(0, a - 8); e = min(e, len(data))
            for m in rb.HEVC_RE.finditer(data, a, e):
                hpos.append(base + m.start()); hlen.append(struct.unpack(">I", data[m.start():m.start() + 4])[0])
            for m in rb.H264_RE.finditer(data, a, e):
                apos.append(base + m.start()); alen.append(struct.unpack(">I", data[m.start():m.start() + 4])[0])
            i = data.find(b"moov", a, e)
            while i != -1:
                if i >= 4 and b"mvhd" in data[i + 4:i + 40]:
                    n = struct.unpack(">I", data[i - 4:i])[0]
                    if 16 < n <= MAX_MOOV:
                        mv = disk.read(base + i - 4, n)
                        h = hashlib.sha1(mv).hexdigest()
                        if h not in moovs: moovs[h] = (base + i - 4, mv)
                i = data.find(b"moov", i + 1, e)
        tail = data[-8:]
        off += len(buf)
        if off - last >= (4 << 30):
            last = off
            print(f"   ...read {off >> 30} of {size >> 30} GB ({(time.time() - t0) / 60:.0f} min), "
                  f"{len(moovs)} indexes found", flush=True)
    os.close(fd)
    d = {"dev": DEV, "moovs": moovs, "hpos": hpos, "hlen": hlen, "apos": apos, "alen": alen}
    with open(CACHE, "wb") as f: pickle.dump(d, f)
    return d

# ------------------------------------------------------------------ main
def main():
    known = rb.load_videos()
    rb.find_starts(known)
    print(f"Loaded {len(known)} already-rebuilt videos (to skip them and protect their pieces).")
    d = scan()
    have = {hashlib.sha1(v["moov"]).hexdigest() for v in known}
    new = []
    for h, (pos, mv) in d["moovs"].items():
        if h in have: continue
        try:
            smp = all_samples(mv)
        except Exception:
            continue
        vid = [(o, s, k) for o, s, k in smp if k]
        if not vid: continue
        new.append({"pos": pos, "moov": mv, "all": smp, "frames": vid,
                    "first": smp[0][0], "total": max(o + s for o, s, _ in smp)})
    print(f"Found {len(d['moovs'])} indexes on the drive; {len(new)} belong to videos not rebuilt yet.")
    if not new: return

    # Catalogue frames of new AND known videos (known ones only to mark pieces as taken).
    vids = new + [{"frames": v["frames"], "known": True} for v in known]
    cat = {}
    for vi, v in enumerate(vids):
        for o, s, k in v["frames"]:
            cat.setdefault((k, s - 4, o % C), []).append((vi, o // C))
    votes = [dict() for _ in vids]
    for kind, P, L in (("hevc", d["hpos"], d["hlen"]), ("h264", d["apos"], d["alen"])):
        for pos, ln in zip(P, L):
            r = (pos - rb.PHASE) % C
            for vi, lc in cat.get((kind, ln, r), ()):
                votes[vi].setdefault(lc, {}).setdefault(pos - r, 0)
                votes[vi][lc][pos - r] += 1
    claimed = {}
    for vi in range(len(vids)):
        for lc, cands in votes[vi].items():
            if len(cands) == 1: claimed.setdefault(next(iter(cands)), set()).add(vi)

    # header templates from known videos: first-sample offset -> header bytes
    templates = {}
    for v in known:
        smp = all_samples(v["moov"])
        if smp: templates.setdefault(smp[0][0], v["head"][:smp[0][0]])

    disk = rb.Disk(DEV)
    os.makedirs(OUT, exist_ok=True)
    order = sorted(range(len(new)), key=lambda i: -new[i]["total"])
    done = good = 0
    t0 = time.time()
    for n, vi in enumerate(order, 1):
        v = new[vi]
        ncl = (v["total"] + C - 1) // C
        mapping = {}
        for lc, cands in votes[vi].items():
            best = sorted(cands.items(), key=lambda kv: (len(claimed.get(kv[0], {vi}) - {vi}) > 0, -kv[1]))
            mapping[lc] = best[0][0]
        verified = set(mapping)
        if len(verified) < max(1, ncl // 20):
            print(f"[{n}/{len(new)}] skipped: only {len(verified)} of {ncl} pieces still on the drive")
            continue
        anchors = sorted(verified)
        for lc in range(ncl):
            if lc in mapping: continue
            i = bisect.bisect_left(anchors, lc)
            opts = [a for a in (anchors[i - 1] if i > 0 else None, anchors[i] if i < len(anchors) else None) if a is not None]
            opts.sort(key=lambda a: abs(a - lc))
            for a in opts:
                ph = mapping[a] + (lc - a) * C
                if ph not in claimed or claimed[ph] == {vi}:
                    mapping[lc] = ph; break
        if shutil.disk_usage(OUT).free < v["total"] + (3 << 30):
            print("\nStopping: your Mac is nearly out of space. Free some and run again."); break
        # header: same layout as a sibling video, with this video's data size
        first = v["first"]
        head = templates.get(first)
        if head is None and templates:
            k = min(templates, key=lambda x: abs(x - first)); t = templates[k]
            ftyp_len = struct.unpack(">I", t[:4])[0]
            free_len = first - 8 - ftyp_len
            head = t[:ftyp_len] + struct.pack(">I", free_len) + b"free" + b"\0" * (free_len - 8) + b"\0" * 8 if free_len >= 8 else None
        if head is None:
            print(f"[{n}/{len(new)}] skipped: no header layout available"); continue
        head = bytearray(head)
        head[first - 8:first - 4] = struct.pack(">I", v["total"] - (first - 8)); head[first - 4:first] = b"mdat"
        tmp = os.path.join(OUT, "_building.mp4")
        with open(tmp, "wb") as f:
            for lc in range(ncl):
                ln = min(C, v["total"] - lc * C)
                blk = disk.read(mapping[lc], ln) if lc in mapping else b"\0" * ln
                if lc == 0: blk = bytes(head) + blk[first:]
                f.write(blk)
            f.write(v["moov"])
        r = subprocess.run([rb.FFPROBE, "-v", "error", "-show_entries",
                            "format=duration:format_tags=creation_time:stream=height", "-of",
                            "default=nw=1", tmp], capture_output=True, text=True).stdout
        dur = float((re.search(r"duration=([\d.]+)", r) or [0, "0"])[1])
        ct = (re.search(r"creation_time=(\S+)", r) or [0, ""])[1]
        h = max([int(x) for x in re.findall(r"height=(\d+)", r)] or [0])
        when = ct[:19].replace("T", "_").replace(":", "") if ct else "unknown-date"
        e = subprocess.run([rb.FFMPEG, "-nostdin", "-v", "error", "-i", tmp, "-map", "0:v:0", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        errs = sum(1 for l in e.splitlines() if "aac" not in l.lower())
        kind = "preview" if h and h <= 720 else "full"
        final = os.path.join(OUT, f"{when}_{dur:.0f}s_{h}p_{kind}_m{v['pos'] // 512}.mp4")
        os.replace(tmp, final)
        pct = 100 * len(mapping) // ncl; conf = 100 * len(verified) // ncl
        done += 1; good += (pct == 100 and errs == 0)
        print(f"[{n}/{len(new)}] {os.path.basename(final)} -> {pct}% placed ({conf}% confirmed by exact frame match)"
              + ("" if errs == 0 else f", {errs} decoder errors"), flush=True)
    user = os.environ.get("SUDO_USER")
    if user:
        subprocess.run(["chown", "-R", user, OUT])
        if os.path.exists(CACHE): subprocess.run(["chown", user, CACHE])
    print(f"\nDone in {(time.time() - t0) / 60:.0f} min. Rebuilt {done} more videos ({good} fully placed with no errors).")
    print(f"They are in {OUT}")

if __name__ == "__main__":
    main()

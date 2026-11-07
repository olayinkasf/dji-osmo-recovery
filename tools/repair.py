#!/usr/bin/env python3
"""
Repair glitches in the recovered full-quality videos, using each video's
preview copy as a guide.

Some 128 KB pieces sit wholly inside a big frame, so their position could not
be confirmed by frame sizes and was guessed from the neighbours. Here, every
such guess is re-checked: each possible position (usually 2-3) is tried, the
affected frames are decoded, and the one that matches the preview picture is
kept. A video is replaced only if it comes out better than before and no frame
gets worse; it keeps its name and dates, and the original is moved to
~/Recovered-USB/before_repair/ first. Videos without glitches are never touched.

Reads the memory card READ-ONLY. Needs the saved scan from rebuild_missing.py.

Usage:  sudo python3 -I ~/Recovered-USB/repair.py ["/path/to/final folder"]
        (default: ~/Movies/DJI Osmo Pocket 3 - recovered)
        add --check to only report which videos have glitches (no drive needed)
"""
import glob, importlib.util, os, pickle, re, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
CHECK = "--check" in sys.argv
_argv = sys.argv; sys.argv = [sys.argv[0], os.environ.get("DEV", "/dev/rdisk2")]
spec = importlib.util.spec_from_file_location("rm", os.path.join(HERE, "rebuild_missing.py"))
rm = importlib.util.module_from_spec(spec); spec.loader.exec_module(rm)
sys.argv = _argv
rb, C = rm.rb, rm.C
HOME = rb.HOME
DEST = os.path.abspath(os.path.expanduser(ARGS[0])) if ARGS else os.path.join(HOME, "Movies", "DJI Osmo Pocket 3 - recovered")
BASE = rb.BASE
W, H = 128, 72
B = 16              # compare 16x16 regions, so damage to part of a frame is caught
BAD = 5.0           # worst-region difference vs preview above this = glitched frame

# ------------------------------------------------------------------ helpers
def video_track(moov):
    """frames [(offset,size)], keyframe set, kind, annex-B parameter sets for the first video track."""
    for t, a, b in rb.boxes(moov, 8, len(moov)):
        if t != b"trak": continue
        m = rb.child(moov, a, b, b"mdia"); m = m and rb.child(moov, *m, b"minf"); st = m and rb.child(moov, *m, b"stbl")
        if not st: continue
        tabs = {tt: (aa, bb) for tt, aa, bb in rb.boxes(moov, *st)}
        if b"stsd" not in tabs: continue
        e0 = tabs[b"stsd"][0] + 8
        kind = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc"}.get(moov[e0 + 4:e0 + 8])
        if not kind: continue
        esz = struct.unpack(">I", moov[e0:e0 + 4])[0]
        cfg = rb.child(moov, e0 + 8 + 78, e0 + esz, b"hvcC" if kind == "hevc" else b"avcC")
        ps = b""
        if cfg:
            q = cfg[0]
            if kind == "hevc":
                q += 22; narr = moov[q]; q += 1
                for _ in range(narr):
                    q += 1; nn = struct.unpack(">H", moov[q:q + 2])[0]; q += 2
                    for _ in range(nn):
                        ln = struct.unpack(">H", moov[q:q + 2])[0]; q += 2
                        ps += b"\0\0\0\1" + moov[q:q + ln]; q += ln
            else:
                q += 5; ns = moov[q] & 31; q += 1
                for _ in range(ns):
                    ln = struct.unpack(">H", moov[q:q + 2])[0]; q += 2; ps += b"\0\0\0\1" + moov[q:q + ln]; q += ln
                npps = moov[q]; q += 1
                for _ in range(npps):
                    ln = struct.unpack(">H", moov[q:q + 2])[0]; q += 2; ps += b"\0\0\0\1" + moov[q:q + ln]; q += ln
        one = b"\0\0\0\0moov" + moov[a - 8:b]
        one = struct.pack(">I", len(one)) + one[4:]
        frames = [(o, s) for o, s, k in rm.all_samples(one) if k]
        keys = None
        if b"stss" in tabs:
            q = tabs[b"stss"][0]; n = struct.unpack(">I", moov[q + 4:q + 8])[0]
            keys = {k - 1 for k in struct.unpack(">%dI" % n, moov[q + 8:q + 8 + 4 * n])}
        return frames, keys, kind, ps
    return None

def annexb(buf):
    out, p = bytearray(), 0
    while p + 4 <= len(buf):
        n = struct.unpack(">I", buf[p:p + 4])[0]
        if n == 0 or p + 4 + n > len(buf): break
        out += b"\0\0\0\1" + buf[p + 4:p + 4 + n]; p += 4 + n
    return bytes(out)

def lowres(cmd_in, data=None, fmt=None):
    cmd = ["ffmpeg", "-nostdin", "-v", "quiet"] + (["-f", fmt] if fmt else []) + ["-i", cmd_in]
    cmd += (["-map", "0:v:0"] if not fmt else []) + ["-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, input=data, capture_output=True).stdout
    return [r[i:i + W * H] for i in range(0, len(r) - W * H + 1, W * H)]

def diff(a, b):
    """Worst 16x16-region mean difference between two low-res frames."""
    worst = 0.0
    for by in range(0, H, B):
        for bx in range(0, W, B):
            t = 0
            for y in range(by, min(H, by + B)):
                r = y * W
                t += sum(abs(x - z) for x, z in zip(a[r + bx:r + min(W, bx + B)], b[r + bx:r + min(W, bx + B)]))
            worst = max(worst, t / (B * B))
    return worst

def glitch_scores(full, preview):
    """Per-frame worst-region difference, computed by ffmpeg (fast)."""
    bw, bh = W // B, (H + B - 1) // B
    fc = (f"[0:v:0]scale={W}:{H}:flags=area,format=gray[a];[1:v:0]scale={W}:{H}:flags=area,format=gray[b];"
          f"[a][b]blend=all_mode=difference:shortest=1,scale={bw}:{bh}:flags=area")
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "quiet", "-i", full, "-i", preview, "-filter_complex", fc,
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    n = bw * bh
    return [max(r[i:i + n]) for i in range(0, len(r) - n + 1, n)]

def recorded(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.strip()
    return r[:19]

PREVIEWS = None
def find_preview(ct):
    global PREVIEWS
    if PREVIEWS is None:
        PREVIEWS = {}
        for p in glob.glob(os.path.join(BASE, "rebuilt2", "*_preview_*.mp4")) + \
                 glob.glob(os.path.join(BASE, "rebuilt_missing", "*_preview_*.mp4")):
            m = re.match(r"(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)(\d\d)_", os.path.basename(p))
            if m: PREVIEWS.setdefault(tuple(int(x) for x in m.groups()), []).append(p)
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", ct)
    if not m: return None
    import calendar
    t = calendar.timegm(tuple(int(x) for x in m.groups()))
    for dt in (0, 1, -1, 2, -2):
        k = time.gmtime(t + dt)[:6]
        if k in PREVIEWS: return PREVIEWS[k][0]
    return None

def frame_diffs(path, preview):
    return glitch_scores(path, preview)

def check_one(f):
    pv = find_preview(recorded(f))
    if not pv: return f, None, 0, 0
    d = glitch_scores(f, pv)
    return f, pv, len(d), sum(1 for x in d if x > BAD)

# ------------------------------------------------------------------ main
def main():
    files = sorted(glob.glob(os.path.join(DEST, "*.MP4")))
    # reuse earlier check results (file name -> preview, frames, glitched frames)
    qfile = os.path.join(BASE, ".quality.tsv")
    done = {}
    if os.path.exists(qfile):
        for line in open(qfile):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4: done[parts[0]] = (parts[1], int(parts[2]), int(parts[3]))
    rest = [f for f in files if os.path.basename(f) not in done]
    print(f"Checking {len(files)} videos in {DEST} against their previews "
          f"({len(files) - len(rest)} already checked earlier)...")
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(6) as ex, open(qfile, "a") as q:
        for i, (f, pv, n, bad) in enumerate(ex.map(check_one, rest, chunksize=2), 1):
            done[os.path.basename(f)] = (os.path.basename(pv) if pv else "", n, bad)
            q.write(f"{os.path.basename(f)}\t{os.path.basename(pv) if pv else ''}\t{n}\t{bad}\n"); q.flush()
            if i % 50 == 0: print(f"   ...{i}/{len(rest)} checked", flush=True)
    todo = []
    for f in files:
        pvn, n, bad = done.get(os.path.basename(f), ("", 0, 0))
        if bad and pvn:
            pv = (glob.glob(os.path.join(BASE, "rebuilt2", pvn)) + glob.glob(os.path.join(BASE, "rebuilt_missing", pvn)))
            if pv: todo.append((f, pv[0], None, bad, n))
    print(f"{len(todo)} videos have glitched frames.")
    for f, pv, prev, bad, n in todo:
        print(f"   {os.path.basename(f)}: {bad} of {n} frames")
    if CHECK or not todo: return

    idx_file = os.path.join(BASE, ".repair_index.pickle")
    if os.path.exists(idx_file):
        print("Using the saved frame index from last time.")
        known = rb.load_videos(); rb.find_starts(known)
        with open(idx_file, "rb") as fh: where, taken = pickle.load(fh)
        scan = allv = keyset = None
    else:
        with open(rm.CACHE, "rb") as fh: scan = pickle.load(fh)
        known = rb.load_videos(); rb.find_starts(known)
        print("Indexing frame positions on the drive...")
        # all frame keys of all videos on the drive, so other videos' pieces count as taken
        allv = []
        for h, (pos, mv) in scan["moovs"].items():
            try: allv.append([(o, s, k) for o, s, k in rm.all_samples(mv) if k])
            except Exception: pass
        keyset = {(k, s - 4, o % C) for v in allv for o, s, k in v}
        where = {}
        for kind, P, L in (("hevc", scan["hpos"], scan["hlen"]), ("h264", scan["apos"], scan["alen"])):
            for pos, ln in zip(P, L):
                r = (pos - rb.PHASE) % C
                if (kind, ln, r) in keyset: where.setdefault((kind, ln, r), []).append(pos - r)
        taken = {}
        for v in allv:
            for o, s, k in v:
                ps = where.get((k, s - 4, o % C), [])
                if len(ps) == 1: taken[ps[0]] = taken.get(ps[0], 0) + 1
        with open(idx_file, "wb") as fh: pickle.dump((where, taken), fh)
        user = os.environ.get("SUDO_USER")
        if user: subprocess.run(["chown", user, idx_file])
    del scan, allv, keyset                      # free memory before the slow part
    tried_file = os.path.join(BASE, ".repair_tried.txt")
    tried = set(open(tried_file).read().split("\n")) if os.path.exists(tried_file) else set()
    before = len(todo)
    todo = [t for t in todo if os.path.basename(t[0]) not in tried]
    if before != len(todo): print(f"Skipping {before - len(todo)} videos already handled before.")
    def mark(f):
        with open(tried_file, "a") as fh: fh.write(os.path.basename(f) + "\n")
    disk = rb.Disk(rb.DEV)
    fixed = 0
    for f, pv, prev, bad0, n in todo:
        prev = lowres(pv)
        buf = bytearray(open(f, "rb").read())
        i = buf.rfind(b"moov") - 4; moov = bytes(buf[i:])
        vt = video_track(moov)
        if not vt: continue
        frames, keys, kind, ps = vt
        ncl = (i + C - 1) // C
        # confirmed pieces of THIS video
        anchor = {}
        for o, s in frames:
            ps_ = where.get((kind, s - 4, o % C), [])
            if len(ps_) == 1: anchor[o // C] = ps_[0]
        starts = {o // C for o, s in frames}
        mine = set(anchor.values())
        def window_err(a, b, tent):
            lo, hi = a * C, (b + 1) * C
            idx = [j for j, (o, s) in enumerate(frames) if o < hi and o + s > lo]
            if not idx: return None
            k = idx[0]
            while keys is not None and k > 0 and k not in keys: k -= 1
            last = min(len(frames) - 1, idx[-1] + 3)
            saved = {c: bytes(buf[c * C:(c + 1) * C]) for c in tent}
            for c, ph in tent.items(): buf[c * C:(c + 1) * C] = disk.read(ph, C)
            stream = ps + b"".join(annexb(bytes(buf[o:o + s])) for o, s in frames[k:last + 1])
            for c, old in saved.items(): buf[c * C:(c + 1) * C] = old
            got = lowres("pipe:0", stream, kind)
            pairs = [(got[j - k], prev[j]) for j in range(k, last + 1) if j - k < len(got) and j < len(prev)]
            if not pairs: return None
            return max(diff(x, y) for x, y in pairs) + 50 * max(0, (last + 1 - k) - len(got))
        changed = 0
        c = 1
        while c < ncl:
            if c in starts or c in anchor:
                c += 1; continue
            a = c
            while c < ncl and c not in starts and c not in anchor: c += 1
            b = c - 1
            if a - 1 not in anchor or b + 1 not in anchor: continue
            P, N = anchor[a - 1], anchor[b + 1]
            opts = []
            for s in range(a, b + 2):
                t = {x: P + (x - a + 1) * C for x in range(a, s)}
                t.update({x: N - (b + 1 - x) * C for x in range(s, b + 1)})
                if any(v in mine or taken.get(v, 0) > 0 for v in t.values()): continue
                if t not in opts: opts.append(t)
            if not opts:
                continue
            cur = window_err(a, b, {})
            best, best_e = None, cur
            for t in opts:
                e = window_err(a, b, t)
                if e is not None and (best_e is None or e < best_e - 0.5): best, best_e = t, e
            # Still bad and only one piece unsure: it may sit alone between two jumps,
            # so try every free piece near its neighbours.
            if (best_e is None or best_e > BAD) and b - a < 8:
                near = set()
                for base in (P, N):
                    for k in range(-48, 49):
                        near.add(base + k * C)
                for v in sorted(near):
                    t = {x: v + (x - a) * C for x in range(a, b + 1)}     # the unsure pieces kept together
                    if v < 0 or any(u in mine or taken.get(u, 0) > 0 for u in t.values()): continue
                    e = window_err(a, b, t)
                    if e is not None and (best_e is None or e < best_e - 0.5): best, best_e = t, e
            if best:
                for x, ph in best.items(): buf[x * C:(x + 1) * C] = disk.read(ph, C)
                changed += 1
        if not changed:
            print(f"{os.path.basename(f)}: no better placement found ({bad0} glitched frames remain)"); mark(f); continue
        tmp = f + ".repairing"
        with open(tmp, "wb") as fh: fh.write(buf)
        d0 = frame_diffs(f, pv)
        d = frame_diffs(tmp, pv)
        bad1 = sum(1 for x in d if x > BAD)
        worse = any(n1 > max(BAD, o1 + 1) for o1, n1 in zip(d0, d))   # no frame may get worse
        if bad1 < bad0 and not worse and len(d) >= len(d0):
            st = os.stat(f)
            backup = os.path.join(BASE, "before_repair"); os.makedirs(backup, exist_ok=True)
            os.rename(f, os.path.join(backup, os.path.basename(f)))      # keep the original
            os.replace(tmp, f); os.utime(f, (st.st_mtime, st.st_mtime))
            user = os.environ.get("SUDO_USER")
            if user: subprocess.run(["chown", user, f])
            fixed += 1
            with open(os.path.join(BASE, ".quality.tsv"), "a") as q:
                q.write(f"{os.path.basename(f)}\t{os.path.basename(pv)}\t{len(d)}\t{bad1}\n")
            print(f"{os.path.basename(f)}: glitched frames {bad0} -> {bad1}", flush=True); mark(f)
        else:
            os.remove(tmp)
            print(f"{os.path.basename(f)}: no improvement ({bad0} glitched frames remain)", flush=True); mark(f)
    print(f"\nDone. Improved {fixed} of {len(todo)} glitched videos. Names and dates are unchanged.")

if __name__ == "__main__":
    main()

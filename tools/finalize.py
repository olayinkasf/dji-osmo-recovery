#!/usr/bin/env python3
"""
Move the recovered full-quality videos to a final folder, with their real
recording date and, where possible, their original DJI file names.

Dates:  taken from the recording time stored inside each video by the camera.
        Both the file's "Created" and "Modified" dates are set to it. The time
        inside the video (used by iCloud, Google Photos, etc.) is untouched.
Names:  the drive is searched (read-only) for the camera's old folder entries,
        e.g. DJI_20261005082536_0012_D.MP4. A video gets its original name
        only if the file size matches exactly. Otherwise it is named from its
        recording time in the same style: DJI_20261005082536_D.MP4

Files are MOVED (no extra space used). A log of every move is written to
the destination folder as recovered_files_log.csv.

Usage:
  python3 -I ~/Recovered-USB/finalize.py ["/path/to/destination"]
  (default destination: ~/Movies/DJI Osmo Pocket 3 - recovered)
  --dry-run       only show what would happen
  --quick-names   also look for original names in the first 4 GB of the drive
                  (needs sudo, about a minute)
  --names         look for original names on the whole drive (needs sudo, slow)
  Without --quick-names/--names the drive is not read at all.
"""
import calendar, csv, glob, importlib.util, os, pickle, re, struct, subprocess, sys, time, shutil

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
DRY = "--dry-run" in sys.argv
NAMES = "--names" in sys.argv            # full drive search for original names (slow)
QUICK = "--quick-names" in sys.argv      # search only the first 4 GB (about a minute)
HOME = os.path.expanduser("~" + os.environ.get("SUDO_USER", ""))
BASE = os.environ.get("RECUP_BASE") or os.path.join(HOME, "Recovered-USB")
DEST = os.path.abspath(os.path.expanduser(ARGS[0])) if ARGS else os.path.join(HOME, "Movies", "DJI Osmo Pocket 3 - recovered")
DEV = os.environ.get("DEV", "/dev/rdisk2")
NAMES_CACHE = os.path.join(BASE, ".drive_names.pickle")
FFPROBE = shutil.which("ffprobe") or "/usr/local/bin/ffprobe"

# ------------------------------------------------------------------ original names
def scan_names():
    """Find old exFAT folder entries for DJI files: {size: [(name, create_time_epoch)]}."""
    if os.path.exists(NAMES_CACHE):
        with open(NAMES_CACHE, "rb") as f: return pickle.load(f)
    if not (NAMES or QUICK):
        return None
    if not os.path.exists(DEV) or not os.access(DEV, os.R_OK):
        print("Memory card not readable (insert it and use sudo to recover original names). "
              "Using names built from the recording time instead.")
        return None
    spec = importlib.util.spec_from_file_location("rb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rebuild2.py"))
    sys_argv = sys.argv; sys.argv = [sys.argv[0], DEV]
    rb = importlib.util.module_from_spec(spec); spec.loader.exec_module(rb)
    sys.argv = sys_argv
    fd = os.open(DEV, os.O_RDONLY)
    size = rb.device_size(DEV, fd)
    if QUICK: size = min(size, 4 << 30)
    print(f"Searching the drive for original file names (read-only, {size / 1e9:.0f} GB)...")
    pat = "DJI_".encode("utf-16-le")
    found = {}
    off, tail, t0, last = 0, b"", time.time(), 0
    while off < size:
        buf, _ = rb.read_span(fd, off, min(32 << 20, size - off))
        if not buf: off += 128 * 1024; tail = b""; continue
        data = tail + buf
        i = data.find(pat)
        while i != -1:
            e = parse_entry(data, i - 2)
            if e: found.setdefault(e[1], set()).add((e[0], e[2]))
            i = data.find(pat, i + 1)
        tail = data[-256:]
        off += len(buf)
        if off - last >= (8 << 30):
            last = off
            print(f"   ...read {off >> 30} of {size >> 30} GB ({(time.time() - t0) / 60:.0f} min), "
                  f"{sum(len(v) for v in found.values())} names found", flush=True)
    os.close(fd)
    res = {k: sorted(v) for k, v in found.items()}
    if not QUICK:
        with open(NAMES_CACHE, "wb") as f: pickle.dump(res, f)
    return res

def exfat_time(ts, cs, utc_off):
    sec = (ts & 0x1F) * 2; mi = (ts >> 5) & 0x3F; hr = (ts >> 11) & 0x1F
    day = (ts >> 16) & 0x1F; mon = (ts >> 21) & 0x0F; yr = 1980 + ((ts >> 25) & 0x7F)
    if not (1 <= mon <= 12 and 1 <= day <= 31): return None
    t = calendar.timegm((yr, mon, day, hr, mi, sec)) + cs // 100
    if utc_off & 0x80:                                   # offset is valid
        q = utc_off & 0x7F
        if q & 0x40: q -= 0x80
        t -= q * 15 * 60
    return t

def parse_entry(d, name_at):
    """name_at = start of the first file-name entry (0xC1). Returns (name, size, created) or None."""
    fe, se = name_at - 64, name_at - 32
    if fe < 0 or name_at + 32 > len(d): return None
    if (d[fe] & 0x7F) != 0x05 or (d[se] & 0x7F) != 0x40 or (d[name_at] & 0x7F) != 0x41: return None
    n_len = d[se + 3]
    if not 5 <= n_len <= 60: return None
    chars = b""
    p = name_at
    while len(chars) < n_len * 2 and p + 32 <= len(d) and (d[p] & 0x7F) == 0x41:
        chars += d[p + 2:p + 32]; p += 32
    try:
        name = chars[:n_len * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"DJI_[\w\-]+\.(MP4|LRF|JPG|DNG|mp4)", name): return None
    size = struct.unpack("<Q", d[se + 24:se + 32])[0]
    ts = struct.unpack("<I", d[fe + 8:fe + 12])[0]
    created = exfat_time(ts, d[fe + 20], d[fe + 22])
    return name, size, created

# ------------------------------------------------------------------ main
def recorded_at(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format_tags=creation_time",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.strip()
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", r)
    if not m: return None
    return calendar.timegm(tuple(int(x) for x in m.groups()))

def main():
    src = sorted(glob.glob(os.path.join(BASE, "rebuilt2", "*_full_*.mp4")) +
                 glob.glob(os.path.join(BASE, "rebuilt_missing", "*_full_*.mp4")))
    if not src:
        sys.exit("No full-quality videos found in rebuilt2/ or rebuilt_missing/.")
    print(f"{len(src)} full-quality videos to move to:\n  {DEST}\n")
    names = scan_names() or {}
    if names:
        print(f"Found {sum(len(v) for v in names.values())} original file names on the drive.\n")
    if not DRY: os.makedirs(DEST, exist_ok=True)
    used, rows = set(os.listdir(DEST)) if os.path.isdir(DEST) else set(), []
    n_orig = n_date = n_nodate = 0
    for p in src:
        t = recorded_at(p)
        size = os.path.getsize(p)
        name = None
        cands = [n for n, _ in names.get(size, []) if n.upper().endswith(".MP4")]
        if len(cands) == 1:
            name = cands[0]; n_orig += 1
        elif t is not None:
            name = "DJI_" + time.strftime("%Y%m%d%H%M%S", time.localtime(t)) + "_D.MP4"; n_date += 1
        else:
            name = os.path.basename(p); n_nodate += 1
        base, ext = os.path.splitext(name)
        k = 2
        while name in used:
            name = f"{base}_{k}{ext}"; k += 1
        used.add(name)
        dst = os.path.join(DEST, name)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "unknown"
        rows.append([name, when, os.path.relpath(p, BASE), size])
        if DRY:
            print(f"  {os.path.basename(p)}  ->  {name}   ({when})")
            continue
        os.rename(p, dst)                         # same disk: instant, no extra space
        if t is not None:
            os.utime(dst, (t, t))                 # sets Modified; also sets Created when earlier
    if not DRY:
        with open(os.path.join(DEST, "recovered_files_log.csv"), "a", newline="") as f:
            w = csv.writer(f)
            if f.tell() == 0: w.writerow(["new name", "recorded (local time)", "came from", "size bytes"])
            w.writerows(rows)
        user = os.environ.get("SUDO_USER")
        if user: subprocess.run(["chown", "-R", user, DEST])
    print(f"\n{'Would move' if DRY else 'Moved'} {len(src)} videos: {n_orig} with their original name, "
          f"{n_date} named from their recording time, {n_nodate} without a date.")
    if not DRY: print(f"Done. They are in: {DEST}")

if __name__ == "__main__":
    main()

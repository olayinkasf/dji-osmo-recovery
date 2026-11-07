# How we rebuilt 795 "lost" DJI videos from a formatted memory card

*November 2026*

Two weeks of travel footage from a DJI Osmo Pocket 3 sat on a 128 GB memory card. One evening the card showed up empty: freshly formatted, 13 MB used, nothing on it. The first message to the AI was short and loud: *can you recover these, and do NOT format it.*

A night and a morning later, 793 full-quality videos and 4 photos were back on the Mac. Every one plays in QuickTime and keeps its original recording date. This post covers how we got there, including the wrong turns, because most of what we learned came from those.

---

## Rule zero: stop writing to the drive

A quick format doesn't erase your files. It erases the **map** of where they are. The data stays on the drive until something new is written over it.

So before anything else:

- **Read-only inspection.** `diskutil info` showed an empty exFAT volume, with macOS already writing Spotlight index files to it. Every write risks overwriting old data.
- **Unmount, don't eject.** Unmounting stops macOS writing to the drive, but the raw device (`/dev/disk2`) stays available to recovery tools.
- **Never "Initialize", never "Repair".** Every tool from here on opened the drive read-only.

## Step 1: PhotoRec, and why it wasn't enough

[PhotoRec](https://www.cgsecurity.org/wiki/PhotoRec) (part of TestDisk) scans a drive sector by sector for file signatures. It found about 1,400 MP4s and 1,500 JPEGs, 26 GB in all.

Almost none of the videos played: "moov atom not found."

An MP4 has two main parts:

- **`mdat`**: the actual video and audio data
- **`moov`**: the index that says where every frame is and how big it is

The Osmo Pocket 3 writes the index *after* the data, when recording stops. PhotoRec stopped each file at the end of `mdat`, so we had 544 files of perfectly good video with no index.

## Step 2: finding the right index for each video

The first fix was naive: look just past the end of each recovered file on the drive and attach whatever index is there. It reported success for 137 files. When we checked, **125 of those had the wrong index**, taken from a neighbouring video.

The better approach used something only the right index can have. Every `moov` lists the exact size of every frame, and the sizes add up to exactly the size of its video data. So we:

1. Scanned the whole drive for every `moov` block.
2. Attached each one only to the recovered file whose data size matched **exactly**.

Wrong pairings became impossible, and 1,352 files got their correct index. They still didn't play properly, though. They'd show the first second and then freeze into garbage.

## Step 3: the camera shuffles two files together

This was the real problem, and it explained why the recovered files felt so fragmented.

The Osmo Pocket 3 records **two files at once**: the full 1080p HEVC video and a 720p H.264 preview (the `.LRF` file DJI's phone app uses). It writes them in **alternating slices of about 1 MB**. On the drive it looks like this:

```
full | full | preview | full | old data | full | preview | full …
```

Normally the file system's map says which slice belongs to which file. That map was gone. PhotoRec assumes a file is one unbroken run, so every recovered video held its first slice correctly, then a mix of preview slices and leftovers.

Checking one 61-second clip confirmed it: only **8 of its 1,842 frames** were valid.

### Putting the slices back in order

The index tells us the exact size of every frame. In HEVC (as stored in MP4) each frame begins with a 4-byte length followed by a 2-byte NAL header. That makes a fingerprint:

> A frame of size *S* that starts at offset *o* in the file must show up on the drive as the bytes `S − 4` at a position whose offset inside its 128 KB cluster is `o mod 131072`.

So the tool:

1. Read the whole drive **once** and recorded every byte pattern that looks like a frame start.
2. Kept only those whose length and position-within-cluster matched a frame in one of the known indexes.
3. Placed each 128 KB cluster of each video wherever its frames were found.

On a real 605 MB stretch of the drive, this placed 4,448 clusters across 8 videos, and **no cluster matched two places**. It also showed why our first attempt failed: the end of one video sat physically *before* its middle on the drive, because the camera filled free gaps wherever it found them.

## Step 4: the bugs that cost us an hour each

Not everything went smoothly. Three bugs are worth sharing because they're easy to repeat.

| Symptom | Cause | Fix |
|---|---|---|
| Every rebuilt video froze after a moment | `lseek(fd, 0, SEEK_END)` returns **0** for a raw disk on macOS, so the "whole drive scan" read nothing | Ask the disk itself (`DKIOCGETBLOCKCOUNT` × block size, or `diskutil info`) |
| The scan slowed from 65 MB/s to 16 MB/s after 44 GB | The rest of the drive was empty, and the regex crawls on long runs of zero bytes | Skip all-zero 64 KB blocks before searching. Results were identical, and speed was back to full card-reader speed |
| The first rebuild tool gave up after ~3 MB | It only searched *forward* from each video's start | Index the whole drive and match frames anywhere |

Each fix was tested on fake drives first. We built fake drives from real footage with slices shuffled in on purpose, and compared the output **byte for byte** with the originals.

## Step 5: the videos whose start was gone

After the rebuild, 179 recordings came back only as the low-quality preview. That included the longest clips, up to 5½ minutes.

These were videos whose **first cluster** PhotoRec never found, so they had no "start" to rebuild from. But their indexes were still on the drive. A second tool:

- collected **every** `moov` on the drive (1,595 of them),
- skipped the ones already used,
- rebuilt the rest using frame matching alone,
- and recreated the tiny file header (`ftyp` + `free` + `mdat` size) from a sibling video from the same camera.

That brought back 201 more full-quality videos.

## Step 6: keeping the dates

For anything you upload to a photo library, **the date is everything**.

The camera stores each clip's recording time inside the file (`creation_time` in UTC). We checked it against the recovered photos. A video recorded at 08:25:36 local time sat on the drive right after a photo taken at 08:25:27. It matched exactly.

A final script then:

- **moved** (not copied) each full-quality video into a clean folder, so no extra space was used,
- named it in DJI's own style from that time, for example `DJI_20261005082536_D.MP4`,
- set both the file's **Created** and **Modified** dates to the recording time,
- and left the time inside the video alone, since that's what iCloud and Google Photos read.

Recovering the *original* names (with DJI's `_0012` counter) is possible by scanning the drive for the old exFAT directory entries. We made it optional, because one more full read of the drive wasn't worth it for a counter.

## Step 7: polishing

### Glitches, repaired with the preview as a guide

About 1 in 5 videos still had a short glitch, usually around one second. The cause was always the same: a 128 KB piece sitting entirely inside a large keyframe, so it had no frame boundary to confirm it. The tool had guessed its position from its neighbours and sometimes picked the wrong side.

Decoders don't always report these errors. Corrupted HEVC slice data often decodes "fine" and just looks wrong. So the check had to look at the **picture**. Each recording also has a 720p preview that came back almost fully confirmed. The repair tool:

1. Compared every frame of the full video with its preview, using the **worst 16×16 region** rather than the frame average. The average missed damage to one part of a keyframe.
2. For every unsure piece, tried each possible position (usually 2–3, sometimes a wider search nearby), decoded just the affected frames, and kept the one closest to the preview.
3. Replaced a video only if it got better **and no single frame got worse**, with the original moved to a backup folder first.

In tests where we broke a good video on purpose, it put every piece back **byte-for-byte identical**, five times out of five.

### "VLC plays it, QuickTime won't"

Fifty videos played in VLC but not in QuickTime, and showed no Finder thumbnail. The first fix was a lossless re-wrap of video and audio. It worked, but it quietly dropped DJI's motion and gyro tracks (`djmd`, `dbgi`), which tools like Gyroflow use.

Going back to find the real cause: the damage was inside `moov/udta/meta`, the camera's ~1 MB **embedded cover thumbnail**, whose last piece had come from the wrong spot on the drive. Removing only that thumbnail fixed QuickTime and kept everything else:

- video and audio unchanged (same checksums),
- all five tracks kept, with DJI's own labels,
- dates unchanged.

One more catch: searching backwards for the text `moov` can match random bytes inside damaged data. Walk the top-level boxes instead.

## Results

| | |
|---|---|
| Full-quality videos recovered | **793** (plus 2 short clips that survive only as previews) |
| Total footage | about 2½ hours |
| Photos | 4 (full 4K stills) |
| Open in QuickTime, with thumbnails | 795 of 795 |
| Original recording dates | kept on every file |
| Writes to the memory card | **zero** |
| Disk space reclaimed after cleanup | about 70 GB |

## What I'd tell anyone in the same spot

1. **Stop using the drive immediately.** Unmount it. Don't let your OS "fix" it.
2. **Generic recovery is a starting point, not the answer.** PhotoRec found everything, but it couldn't untangle a camera that writes two files at once.
3. **Use what the file format guarantees.** Exact frame sizes and NAL headers turned guesswork into proof.
4. **Check against something independent.** Decoders stay quiet about broken frames. The preview copy was an honest second opinion.
5. **Never overwrite without a backup, and test on fake data first.** Every fix was proven on deliberately broken copies before it touched real files.
6. **Keep the dates.** It's the detail that matters most once the files are back in your library.

And keep the memory card untouched until everything is backed up twice.

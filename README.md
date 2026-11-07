# dji-osmo-recovery

Scripts I used to get 793 videos back from a formatted memory card from a DJI Osmo Pocket 3. PhotoRec found the files, but almost none of them played. These fix that.

How it went, including the parts that went wrong: [docs/recovering-dji-videos-from-a-formatted-memory-card.md](docs/recovering-dji-videos-from-a-formatted-memory-card.md)

## The problem these tools solve

After a quick format, file carvers like PhotoRec find the videos, but the files don't play, or they freeze after a second:

1. The index (`moov`) is missing. The camera writes it *after* the video data, and the carver stops at the end of the data.
2. The video is interleaved with its preview. The Osmo Pocket 3 records the full HEVC video and a 720p H.264 preview at the same time, in alternating ~1 MB slices. Without the file system's map, a carver treats each file as one continuous run, so everything after the first slice is a mix of both files and leftovers.

These tools rebuild each video using what the MP4 format guarantees. Every frame's exact size is in the index, and every frame starts with a 4-byte length plus a NAL header. A frame of size *S* at file offset *o* must appear on the drive as the bytes `S − 4`, at a position with the same offset inside its 128 KB cluster. Matching every frame that way puts each cluster back where it belongs, wherever the camera wrote it.

## Safety

- The drive is only ever opened read-only (`O_RDONLY` on the raw device). Nothing is written to it.
- Unmount it (`diskutil unmount /Volumes/…`), don't eject, and never let macOS "Initialize" or "Repair" it.
- Files are only replaced when the new version is verified better, and the original is moved to a backup folder first.

## Requirements

- macOS (raw device access via `/dev/rdiskN`, `diskutil`, `avmediainfo`)
- Python 3.9+, standard library only
- `ffmpeg` / `ffprobe` (`brew install ffmpeg`)
- [PhotoRec / TestDisk](https://www.cgsecurity.org/wiki/PhotoRec) (`brew install testdisk`)
- Free disk space of roughly 2× the footage

## Pipeline

All tools default to working in `~/Recovered-USB` (override with `RECUP_BASE=/path`). They assume the drive is `/dev/rdisk2` (pass another as the first argument) with its partition starting at sector 64 (override with `PART_OFFSET=bytes`). Check yours with `diskutil list`.

| Step | Command | What it does |
|---|---|---|
| 1. Carve | `sudo photorec /d ~/Recovered-USB/recup /dev/rdisk2` | Finds file starts (pick the partition, then Other, then Whole). Produces `recup.N/f<sector>.mp4` files with no index. |
| 2. Find indexes | `sudo python3 -I tools/fix_moov2.py` | Scans the drive for every `moov` and attaches each to the carved file whose data size matches exactly. Output: `fixed2/` |
| 3. Rebuild | `sudo python3 -I tools/rebuild2.py` | Reads the drive once, matches every frame header to its video by exact size + position inside the cluster, and reassembles each video. Output: `rebuilt2/` |
| 4. Rebuild the rest | `sudo python3 -I tools/rebuild_missing.py` | For videos whose *start* was lost (so step 1 never found them): rebuilds from every leftover `moov` on the drive and recreates the file header. Output: `rebuilt_missing/` |
| 5. Finalize | `python3 -I tools/finalize.py [dest]` | Moves the full-quality videos to `dest` (default `~/Movies/DJI Osmo Pocket 3 - recovered`), named `DJI_YYYYMMDDHHMMSS_D.MP4` from the recording time, with Created/Modified dates set to the recording time. `--quick-names` / `--names` look for the original DJI file names on the drive. `--dry-run` shows the plan. |
| 6. Repair glitches | `sudo python3 -I tools/repair.py [dest]` | Compares every video with its 720p preview (worst 16×16 region per frame). For pieces whose position couldn't be confirmed, it tries each candidate, decodes the affected frames, and keeps the one that matches the preview. Originals go to `before_repair/`. `--check` only reports. |

Drive scans are cached, so re-running a step skips re-reading the drive.

### If QuickTime won't open some videos but VLC will

In our case the camera's ~1 MB embedded cover thumbnail (`moov/udta/meta`) was damaged. Removing only that box fixed QuickTime and Finder thumbnails, while keeping video, audio, timecode and DJI's motion/gyro tracks (`djmd`, `dbgi`). Avoid `ffmpeg -c copy` re-wraps for this: they drop the DJI sensor tracks. Also walk the top-level boxes to find `moov`; `rfind(b"moov")` can match random bytes inside damaged data.

## Results on the original drive

| | |
|---|---|
| Full-quality videos recovered | 793 (+2 clips that survived only as previews) |
| Open in QuickTime | 795 / 795 |
| Recording dates preserved | all |
| Writes to the memory card | 0 |

## Caveats

- Built for and tested on one drive: DJI Osmo Pocket 3, exFAT, 128 KB clusters. Other DJI cameras that write an `.LRF` preview alongside the video likely behave similarly, but expect to adjust.
- A cluster that sits entirely inside a large frame has no frame boundary to confirm it. Its position is inferred, and that's what `repair.py` verifies against the preview.
- Footage that was overwritten before the format can't be recovered by anything.

## How this was made

I wrote these with Claude in one long session. It wrote most of the code; I drove, pushed back when results looked wrong, and set the rules about not touching the drive. The write-up in `docs/` has the full story.

## License

MIT

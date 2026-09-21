# Media ingest and archive (planned side project)

> **Status: planned, not built.** Nothing here is in the code. This is research and a design to
> build from, written before any hardware is bought, so the expensive decisions get made on
> purpose. The Pico companion is unchanged and stays paused.

The idea: a Raspberry Pi already works as a camera media upload device (a place to drop a file). You
are getting into film and production and may add a hard disk for archives. **The media Pis are their own
system.** They share no machine, disk, queue or CI runner with anything Nightjar runs, so nothing in this
document is a trade-off against staging. Nightjar only watches them.

## The short answers

| Question | Answer |
|---|---|
| On a shoot, how does footage get from the card to the archive? | **Two tiers.** Copy to a local SSD on site, verify, and only then clear the card. Upload to the archive at home once the local copy is confirmed. See [On location](#on-location-two-storage-tiers). |
| Can LTE carry a RAW shoot day? | **No.** Hundreds of GB against a few Mbps of uplink. LTE carries chosen selects or proxies; the drive carries the rest home. |
| Where does the archive live? | **On the archive Pi, on its own disk.** Nothing else runs there. |
| Can a Pi hold it? | **Yes, with an external disk.** A shoot day is hundreds of GB, so size the disk to the footage. |
| How does Nightjar relate to it? | **It watches and never controls.** The Pis push state in, and a silent Pi is shown as `reporter silent`. |

## What a Raspberry Pi can be

Ranked for the media project, not in general. Your idea is first.

| Role | Fit | Why |
|---|---|---|
| **Media ingest and archive appliance** | Strong | Gigabit Ethernet and USB 3 disks are enough for an archive. |
| **Offsite copy of the archive** | Strong | A second copy in a different place is what makes an archive safe. Pulls archive changes on a schedule. |
| **Cabinet and drive sensor node** | Good, cheap | Temperature and humidity sensors plus SMART readings for the archive drive, posted like `thermal_probe.py`. |
| Transcoding proxies | Avoid | Verify your model's video hardware before planning this. Your editing machine is the better place. |

## The archive Pi

### How big film footage is

Figures below are **Apple's own target data rates** (ProRes white paper, April 2022, Target Data Rates
tables, pages 19 and 20). They are exact for ProRes and only a guide for your camera, whose native format
may be much smaller (long-GOP H.264 or H.265) or larger (RAW).

| Format | Mb/s | GB per hour | Hours on 4 TB |
|---|---|---|---|
| ProRes 422 HQ, UHD 3840×2160, 24p | 707 | 318 | about 12.6 |
| ProRes 422 HQ, UHD, 30p | 884 | 398 | about 10 |
| ProRes 422, UHD, 24p | 471 | 212 | about 18.9 |
| ProRes 422 HQ, 1080p, 24p | 176 | 79 | about 51 |
| ProRes 422 Proxy, UHD, 24p | 145 | 65 | about 62 |

**Estimate, not verified:** camera-native 4K in H.265 is often in the region of 100 to 200 Mb/s, which
is about 45 to 90 GB per hour. Check your camera's real bitrate before sizing anything.

Two hours of UHD ProRes 422 HQ is about 636 GB.

### Hardware

A Raspberry Pi 4 or 5 with a USB 3 disk is a capable file server. The Pi 5 adds a PCIe 2.0 x1 connection for
an NVMe drive, two USB 3.0 ports that run at full speed together, and gigabit Ethernet. It needs the official
27 W supply: without it USB current is limited to 600 mA, and a bus-powered hard disk will brown out, so use
a powered enclosure. Community tests report roughly 110 MB/s over Samba on a Pi 5 with an SSD and 50 to
80 MB/s with a USB hard disk.

| Pressure | What happens | Mitigation |
|---|---|---|
| **Network** | One hour of UHD ProRes 422 HQ (318 GB) takes about **53 minutes at 100 MB/s**, and about **88 minutes at 60 MB/s**, which is what a USB hard disk over Samba can do (community-reported 50 to 80 MB/s). | Accept that a shoot day is an overnight job. |
| **Drive power** | A bus-powered disk can brown out the Pi. | Powered enclosure and the official supply. |
| **Drive health** | A failing disk gives little warning. | Report SMART readings to Nightjar. |

### How files reach it

Both options are decided later:

- **A drop folder** (Samba or SFTP), copied in with `rsync` or `rclone`. Simplest, and probably right for a
  card-reader-and-laptop workflow.
- **Resumable HTTP uploads with [tus](https://tus.io/protocols/resumable-upload)**, served by
  [tusd](https://github.com/tus/tusd) (the reference server, in Go). Better for large files over an
  unreliable link, because an interrupted upload continues where it stopped.

## On location: two storage tiers

Everything above assumes files arrive at an archive Pi that stays at home. A shoot day is different: cards fill on
site, and the next shot needs an empty card. Two jobs get confused here, and they run on very different clocks.

| Job | Speed set by | Gates shooting? |
|---|---|---|
| **Clear the card**: copy it to a fast local drive, verify, then free the card | Card reader and the local drive | **Yes.** It is the only job that does. |
| **Upload over LTE**: send footage to the home archive | The LTE uplink | **No.** It must never be a reason a card stays full. |

So there are two storage tiers, and the rule between them is one sentence: **save to local first, and upload to
remote only once the local copy is confirmed.**

| Tier | Where | Medium | Job |
|---|---|---|---|
| **1. Local** | On site, on the field Pi | **SSD** | Take the card offload fast and hold the day's footage until it is safe elsewhere |
| **2. Remote** | At home, on the archive Pi (design C above) | **Hard disk** | The archive. Speed matters less, capacity and cost matter more |

### The order of events for every file

| Step | State | What is allowed now |
|---|---|---|
| 1 | **On the card** | Nothing is safe. Do not format. |
| 2 | **Copied to local** | Still not safe. A copy nobody has checked is a guess. |
| 3 | **Verified on local** (read back, checksum matches) | **The card may be cleared.** This is the only gate for shooting. |
| 4 | **Queued for upload** | Waits for a link, or for the drive to travel home. Never blocks step 3 for the next card. |
| 5 | **Uploaded, and verified on remote** | **Only now may the local copy be freed.** |

Two things follow from that table:

- **The card is freed at step 3, and local space at step 5.** Local is not a cache to be emptied when full. If it
  fills before uploads catch up, the answer is a bigger SSD or a second one, never deleting an unconfirmed copy.
- **Verification is part of the clock.** Read-back roughly doubles a copy's time. Skipping it is how footage is
  lost, so plan the offload time with it included.

### What LTE can and cannot carry

Peak LTE uplink by modem category, from a published table (real speeds are lower and shared, and the table is
peak or headline rates):

| Modem category | Peak uplink | 20 GB takes |
|---|---|---|
| Cat 1 | 5 Mbps | about 8.9 hours |
| Cat 4 | 50 Mbps | about 53 minutes at best |
| Cat 7 | 150 Mbps | about 18 minutes at best |

Which category a given modem is, and what it actually gets where you shoot, is **unmeasured**: test on site.

RAW footage is enormous. Apple states ProRes RAW falls between ProRes 422 and ProRes 422 HQ, and ProRes RAW HQ
between 422 HQ and 4444. Combined with the verified table above, that is **roughly 212 to 318 GB per hour for
ProRes RAW and 318 to 477 GB per hour for RAW HQ** at UHD 24p. That figure is my inference from those two
statements, not a published number, and other RAW formats and stills differ. A three-hour shoot is about 636 to
954 GB, which is over 28 hours at 50 Mbps even at peak. **LTE cannot carry a shoot day.** It can carry about
20 GB of chosen material.

So over LTE, upload **only what is chosen**: proxies, selects, or one hero clip, marked by the operator. The full
RAW travels on the drive and reaches the archive at home over gigabit, where 636 GB takes about 1.8 hours at
100 MB/s. This is still the same rule: the uploaded set is deleted from local only once the remote copy verifies,
and the rest stays on local until the drive is home.

### How fast the card clears

Estimated time to copy, before verification. **The rates are my working figures, not measurements:** about
400 MB/s for a fast SSD on a Pi 5's USB 3 port (a ceiling), and about 100 MB/s for a portable hard disk.
Double it for read-back.

| Card | To a hard disk (about 100 MB/s) | To an SSD (about 400 MB/s) |
|---|---|---|
| 20 GB | about 3.5 min | about 1 min |
| 128 GB | about 21 min | about 5 min |
| 256 GB | about 43 min | about 11 min |
| 512 GB | about 85 min | about 21 min |

That is why tier 1 is an SSD. Around a four-times difference decides whether a card is back in the camera in
twenty minutes or ninety, and an SSD also survives being carried.

### Rules for the field tier

1. **The local tier is an SSD, and not an SMR hard disk.** Many large portable hard disks use shingled recording
   and can stall badly on sustained writes. Check the model before buying.
2. **Own more cards than you fill in a day.** Offload one while you shoot with another.
3. **A Pi is a good background worker, not the fastest offloader.** Its USB ports cap the rate above. A laptop with
   a fast NVMe drive is often quicker for on-site offload. The field Pi's strength is the unattended work:
   verifying, holding the queue, and sending chosen files over LTE. Whether to offload with the Pi or a laptop
   is an open question below.
4. **Power.** A Pi 5 needs its 27 W supply, and a bus-powered drive adds current on top. Check the whole load
   against a battery or car supply before a shoot.
5. **Never format a card the software has not confirmed.** The tool shows "verified, safe to clear" per card,
   and a person still formats it. Nothing formats automatically.

### What Nightjar adds for this

Nightjar still only watches. The field Pi pushes its state the way the stack reporter does, and the Media tab
shows the two tiers side by side.

| Nightjar would show | Severity when wrong |
|---|---|
| Cards **awaiting verification**, and the oldest one waiting | P2 if a card has waited too long |
| **A verification failure** (a checksum mismatch on local) | P2, and it stays open until a person looks |
| **Local free space** falling toward zero while unconfirmed files remain | P2 |
| **Upload backlog** and the oldest unconfirmed file | P3 |
| **Field Pi silent** | Shown as `reporter silent`, never as healthy |

None of these is P1. A media problem never wakes the phone.

## Home streaming (Jellyfin)

**Built (chunk 3 extension).** Jellyfin runs on the ThinkCentre, with its library on an external SSD, and
people watch in Jellyfin's own apps or a browser. Nightjar never plays, stores or controls video. This is
separate from the archive Pi above, which stays its own system. Production runs on the VPS, so a stream can
slow staging but never a client's live site.

| Piece | Where |
|---|---|
| Reporter: polls Jellyfin's `/Sessions` and `/System/Info/Public` on localhost, pushes a snapshot | `client/jellyfin_reporter.py`, run by `deploy/media/jellyfin-reporter.timer` |
| Ingest: `POST /ingest/{key}/media`, signed like every other source | `hub/media.py` |
| Feeds: `GET /api/media` (live) and `GET /api/media/history` | `hub/media.py` |
| Tables: `media_reports` (live snapshots, pruned after 7 days), `media_plays` (history) | migration `0002` |
| Console: the **Media** tab, and a panel on the idle screen | `console/src/views/Media.jsx`, `views/Idle.jsx` |

The media server is a site with `env = media`. It is left out of `/api/sites/health`, so it never appears among
client sites, the verdict or the wall's site cards. The Jellyfin API key is a broad admin key: it stays on the
media machine and is never sent to the hub.

**What it records.** A viewing is one session watching one item. It continues while that session keeps
reporting, and a gap over 10 minutes starts a new one. `watched_s` counts real watching: paused time and
reporter gaps are not credited. History is personal data, so it is kept for `MEDIA_HISTORY_DAYS` (default 365)
and then deleted.

**What is shown where**

| | Console Media tab | Idle screen |
|---|---|---|
| Server up, not responding, disk nearly full, or `reporter silent` | yes | yes |
| Number of streams, and library disk free | yes | yes |
| Who is watching, what, on which device, direct play or transcode | yes | only after the fingerprint reveal |
| History: watched by person, most watched, recent viewings (7 or 30 days) | yes | no |

**Problems** open P2 issues, never P1: `MediaServerDown` when Jellyfin stops answering and `MediaDiskLow` under
10% free. They are edge-triggered (one issue when it starts, not one per report) and resolve themselves after
`STACK_HEALTHY_MIN` minutes of healthy reports. A quiet reporter is shown as `reporter silent` and its old
snapshot claims no streams. A stream starting is never an alert.

**Demo card.** Until a real media server reports, the dashboard tile and the idle-screen panel show sample data
so the layout can be judged. It is tagged `demo` with a note, is never counted in any verdict or alert, and
disappears the moment the first real report arrives (`demoMedia` in `console/src/media.js`). It is not shown
while the hub is unreachable.

**Not yet verified.** The reporter's field names follow Jellyfin's documented Sessions API and were tested
against a fake server, not a live Jellyfin. Check them on the real server before relying on the numbers. Whether
hardware transcoding works on the M920q's Intel graphics is also unmeasured.

## What Nightjar does, and does not do

Nightjar is not the media manager. Tools for that already exist, and rebuilding them is out of scope. Its
job stays narrow: **watch, decide, deliver.**

| Nightjar would | Nightjar would not |
|---|---|
| Show a **Media** tab: uploads in progress, archive disk free, last verified, files not yet verified | Store, transcode, browse or play footage |
| Raise issues: archive disk low, a checksum mismatch, an upload stalled, the Pi gone silent | Wake you for any of them: **media problems are P2 or P3, never P1** |
| Reuse the existing patterns: a reporter that pushes in, silence shown as `reporter silent`, edge-triggered issues | Reach into the Pi. It is pushed to, never controlled from the hub |

## Integrity and backup

Footage is irreplaceable, so this matters more than speed.

- **RAID is not a backup, and one archive disk is not a backup.** Plan a second copy in another place. The
  Pi as an offsite replica is the strongest use for a second one.
- **Verify by checksum before you wipe a card.** Copy, compute a checksum manifest, compare, and only then
  format the card. `rsync --checksum`, `rclone check`, or a fast hash such as BLAKE3 or xxHash.
- **Watch the drive.** SMART readings (`smartctl`) reported to Nightjar give early warning that a disk is
  failing.
- **A journaling Linux filesystem for the archive** (ext4 or XFS). exFAT is for camera cards, not for storage
  you cannot afford to corrupt.

## Open questions

Nothing is decided until these are answered.

1. **Which Raspberry Pi models do you have** (4 or 5), and how much RAM?
2. **What archive disk**: size, 2.5" or 3.5", hard disk or SSD?
3. **What do your cameras record?** ProRes, RAW or H.264/H.265, and roughly how many hours on a shoot day.
   This sets the storage size and whether overnight transfers are enough.
4. **How do files reach the archive Pi:** a card reader on the Pi, or uploads from a laptop or phone?
5. **Is there a plan for a second copy in another place?**
6. **On a shoot day: is it video RAW or stills, and from which camera?** Which format sets the real GB per hour.
6. **Which cards, how many, and what sizes**, and how many hours do you shoot on a busy day?
6. **What LTE speed do you actually get where you shoot**, and which modem? Measure it on site.
6. **Offload with the field Pi or a laptop?** The Pi is good unattended. A laptop with fast NVMe is usually
    quicker to clear a card.
6. **How big does the local SSD need to be?** At least the largest day you expect, since local is only freed
    after the remote copy verifies.

## Sources, and how much to trust each

| Claim | Confidence | Source |
|---|---|---|
| ProRes data rates and GB per hour | **Verified in the source tables** | [Apple ProRes white paper, April 2022](https://www.apple.com/final-cut-pro/docs/Apple_ProRes.pdf) |
| tusd is the reference tus server | **Read in the docs** | [tusd](https://github.com/tus/tusd), [tus protocol](https://tus.io/protocols/resumable-upload) |
| Pi 5 PCIe 2.0 x1, two USB 3.0 ports, gigabit Ethernet, 27 W supply, 600 mA USB default | Product page and power guides | [Raspberry Pi 5](https://www.raspberrypi.com/products/raspberry-pi-5/), [powering the Pi 5](https://bret.dk/how-to-power-the-raspberry-pi-5-a-complete-guide/), [Pi forum](https://forums.raspberrypi.com/viewtopic.php?t=383340) |
| Samba speeds of 50 to 80 MB/s (hard disk) and about 110 MB/s (Pi 5 with SSD) | **Community-reported**, not lab-tested | [PiCockpit](https://picockpit.com/raspberry-pi/turn-your-raspberry-pi-into-a-nas-with-samba/), [Zbotic](https://zbotic.in/raspberry-pi-file-server-samba-nas-for-home-network/) |
| Camera-native H.265 at 100 to 200 Mb/s | **My estimate**, unverified | none |
| LTE peak uplink by UE category | **Read in the table**. Peak or headline rates, so real speeds are lower | [Electronics Notes](https://www.electronics-notes.com/articles/connectivity/4g-lte-long-term-evolution/ue-categories-classes.php) |
| ProRes RAW sits between ProRes 422 and 422 HQ (RAW HQ between 422 HQ and 4444) | **Read in Apple's chart**. The GB per hour derived from it is **my inference** | [Apple ProRes RAW white paper](https://www.apple.com/final-cut-pro/docs/Apple_ProRes_RAW.pdf) |
| About 400 MB/s SSD on a Pi 5 USB port and about 100 MB/s portable hard disk, and the card-clearing times built on them | **My working figures**, not measured | none |
| Whether a Pi can transcode proxies acceptably | **Not researched**: verify for your model | none |

A note on process: the first search summary I got claimed ProRes 422 HQ UHD 30p is 629 Mb/s and 283 GB per
hour. Reading Apple's table showed that was wrong (629 Mb/s is plain ProRes 422 at 4096×2160 30p). The
figures above are the verified ones.

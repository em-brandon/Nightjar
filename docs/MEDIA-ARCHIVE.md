# Media ingest and archive (planned side project)

> **Status: planned, not built.** Nothing here is in the code. This is research and a design to
> build from, written before any hardware is bought, so the expensive decisions get made on
> purpose. The Pico companion is unchanged and stays paused.

The idea: a Raspberry Pi already works as a camera media upload device (a place to drop a file). You
are getting into film and production, may add a hard disk for archives, and want to know whether the
ThinkCentre can host this, whether it will strain storage, and whether staging can be paused while an
upload arrives or an upload rejected while a merge to `main` is running.

## The short answers

| Question | Answer |
|---|---|
| Can the ThinkCentre do it? | **Yes, but only with its own external drive.** Its internal storage cannot hold a film archive. |
| Will it strain storage? | **Not if media has its own drive and filesystem.** On the staging volume it will, and a full disk would take Postgres and Gitea down with it. |
| Better place for it? | **The Raspberry Pi itself**, with the archive disk attached to it. Then media never touches the machine that runs staging. |
| Pause staging during an upload? | **Queue staging builds, don't pause running stacks.** Pausing a container frees no memory. |
| Reject uploads during a merge to `main`? | **Defer them, don't drop them.** A resumable protocol turns "reject now" into "retry in a minute" with nothing lost. |
| One rule above the rest | **Media must never delay a production deploy.** |

## What a Raspberry Pi can be

Ranked for this project, not in general. Your idea is first.

| Role | Fit | Why |
|---|---|---|
| **Media ingest and archive appliance** | Strong | Gigabit Ethernet and USB 3 disks are enough for an archive. Keeps large files off the staging machine entirely. |
| **Offsite copy of the archive and the backups** | Strong | A second copy in a different place is what makes an archive safe. Pulls `pg_dump` and archive changes on a schedule. |
| **Wall display driver** (Chromium kiosk) | Good option | Takes the HDMI display off the ThinkCentre, so a staging problem cannot blank the wall. The current design puts the kiosk on the ThinkCentre. Undecided. |
| **Cabinet and drive sensor node** | Good, cheap | Temperature and humidity sensors plus SMART readings for the archive drive, posted like `thermal_probe.py`. |
| **Independent uptime probe** | Modest | The reason for a second probe (#11) is a different network. The VPS already provides one. A Pi on a *third* network adds detail. One on the same home ISP as the ThinkCentre adds nothing. |
| Tailscale subnet router or DNS | Useful, but not Nightjar | Infrastructure. Dockge already relies on Tailscale. |
| Extra CI runner | Weak | Only worth it if a client ships ARM images. The ThinkCentre is stronger. |
| Transcoding proxies | Avoid | Verify your model's video hardware before planning this. The ThinkCentre's Intel graphics or your editing machine is the better place. |

## Can the ThinkCentre host it?

### What it can hold

Per Lenovo's spec sheet, the M920q takes one M.2 2242 PCIe NVMe drive (up to 1 TB listed) and one
2.5" bay (up to 1 TB hard drive or 256 GB SSD). **Installing a 2.5" drive rules out the PCIe expansion
card**, which was one of the reasons the M920q was chosen over the alternatives (#11). A shoot day does not
fit in 1 TB, so an archive means an external drive whatever else is decided.

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

Two hours of UHD ProRes 422 HQ is about 636 GB. That is one shoot day filling most of an internal drive.

### Where it would strain the ThinkCentre

| Pressure | What happens | Mitigation |
|---|---|---|
| **Disk full** | The media volume shares a disk with Postgres, Docker and Gitea. When it fills, all of them fail together. | A **separate drive and filesystem** for media, mounted on its own path, so its blast radius is only media. |
| **Disk I/O** | Big sequential writes slow staging databases and builds on the same device. | Separate physical disk. Lower the priority of the copy process. |
| **Network** | A gigabit link tops out around 110 MB/s in practice. One hour of UHD ProRes 422 HQ (318 GB) takes about **53 minutes at 100 MB/s**, and about **88 minutes at 60 MB/s**, which is what a USB hard disk over Samba can do (community-reported 50 to 80 MB/s). | Schedule big transfers. Accept that a shoot day is an overnight job. |
| **CPU and heat** | Checksumming uses a core. Transcoding proxies uses many. The cabinet's target is CPU under 80 °C under sustained load. | Run heavy work off-peak with a CPU cap, or do it elsewhere. |
| **Drive power and heat** | A hard disk adds heat and spin-up current inside a cabinet whose airflow is still unmeasured. | A powered enclosure, and include the drive in the tissue test and thermal baseline before fabrication. |

**Verdict.** Possible: yes, with a dedicated external drive. Wise: not on the staging volume. The strain is
manageable only if media is isolated, and isolation is easier if it lives on a different machine.

## Recommended architecture

| | Where files land | Strain on the ThinkCentre | Verdict |
|---|---|---|---|
| **A. Pi ingests, then forwards** | Pi's own disk, then copied to an archive drive | Only during the copy, which can be scheduled | Good if you want the Pi to be a small buffer |
| **B. Straight to the ThinkCentre** | A dedicated external drive on the ThinkCentre | Full contention with staging and builds | Needs the gate below. Simplest single box |
| **C. The Pi is the archive** | The disk attached to the Pi | **None.** Nightjar only watches it | **Start here** |

**Start with C.** A Raspberry Pi 4 or 5 with a USB 3 disk is a capable file server. The Pi 5 adds a PCIe 2.0
x1 connection for an NVMe drive, two USB 3.0 ports that run at full speed together, and gigabit Ethernet. It
needs the official 27 W supply: without it USB current is limited to 600 mA, and a bus-powered hard disk
will brown out, so use a powered enclosure. Community tests report roughly 110 MB/s over Samba on a Pi 5
with an SSD and 50 to 80 MB/s with a USB hard disk.

Under C there is no strain to manage and the gate below is not needed. It becomes necessary only if you
choose B, or later want the archive copied to the ThinkCentre.

For how files reach it, two options, both decided later:

- **A drop folder** (Samba or SFTP), copied in with `rsync` or `rclone`. Simplest, and probably right for a
  card-reader-and-laptop workflow.
- **Resumable HTTP uploads with [tus](https://tus.io/protocols/resumable-upload)**, served by
  [tusd](https://github.com/tus/tusd) (the reference server, in Go). Better for large files over an
  unreliable link, because an interrupted upload continues where it stopped.

## The gate: production never waits

Your two rules (pause staging when an upload arrives, reject uploads during a merge to `main`) are the right
instinct. They are one idea, and it needs an order of priority.

| Priority | Lane | Under contention |
|---|---|---|
| 1 | **Production deploy** (a merge to `main`) | **Never waits and is never throttled.** |
| 2 | **Media upload** | New uploads are **deferred** while a production deploy runs. Uploads already in progress are **throttled, not killed.** |
| 3 | **Staging build** | **Queued** while an upload is active. Running staging stacks stay up. |
| 4 | **Background work** (checksum verification, proxies, archive copy) | Runs only when lanes 1 to 3 are idle. |

Three refinements to your wording:

1. **Queue builds, don't pause stacks.** `docker pause` freezes a container but frees no memory. The load is
   the *build*, so that is what to hold back.
2. **Defer, don't reject.** Killing a 200 GB upload at 95% is a bad outcome. With a resumable protocol, a
   refusal means "come back in a minute" and nothing is lost.
3. **Throttle in-flight uploads during a deploy** instead of dropping them.

### How it would work

The decision has to be **local to the machine**, not asked of the hub. The hub is on a VPS, and a hook that
waits on an internet round-trip can time out or fail. So:

- A small state directory on the ThinkCentre holds lane files: a `production-deploy` lock and a
  `media-active` counter.
- The production deploy job creates its lock at the start and removes it at the end (even on failure). It
  never checks anything first.
- tusd's **`pre-create` hook** checks for that lock and refuses new uploads. The hook can reject with a custom
  HTTP status and body (`RejectUpload`). Two limits from tusd's docs: hooks are blocking with a **15-second
  default timeout**, so the check must be a fast local lookup, and the docs do not confirm a `Retry-After`
  header, so the client is told the retry interval in the response body.
- tusd's `post-create`, `post-finish` and `post-terminate` hooks keep the `media-active` counter.
- Staging jobs in the CI runner begin with a wait-for-lane step that holds while `media-active` is set.
- Every lock has a **time limit**, so a crashed job cannot block uploads forever.
- **The gate fails open.** If it breaks, uploads and staging proceed. It is never allowed to block a
  production deploy, the same way nothing may suppress an alert.

The hub is told what is happening for display only, fire and forget.

## What Nightjar does, and does not do

Nightjar is not the media manager. Tools for that already exist, and rebuilding them is out of scope. Its
job stays narrow: **watch, decide, deliver.**

| Nightjar would | Nightjar would not |
|---|---|
| Show a **Media** tab: uploads in progress, archive disk free, last verified, files not yet verified | Store, transcode, browse or play footage |
| Show which lane is busy, for example "media uploading: staging builds queued" | Decide the lanes (that is local, above) |
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
- **The cabinet:** include the drive in the open-air thermal baseline and the tissue test before anything is
  cut.

## Open questions

Nothing is decided until these are answered.

1. **Which Raspberry Pi models do you have** (4 or 5), and how much RAM?
2. **What archive disk**: size, 2.5" or 3.5", hard disk or SSD?
3. **What do your cameras record?** ProRes, RAW or H.264/H.265, and roughly how many hours on a shoot day.
   This sets the storage size and whether overnight transfers are enough.
4. **Where should the archive live:** on the Pi (recommended), or on the ThinkCentre?
5. **How do files reach the Pi:** a card reader on the Pi, or uploads from a laptop or phone?
6. **Is there a plan for a second copy in another place?**

## Sources, and how much to trust each

| Claim | Confidence | Source |
|---|---|---|
| ProRes data rates and GB per hour | **Verified in the source tables** | [Apple ProRes white paper, April 2022](https://www.apple.com/final-cut-pro/docs/Apple_ProRes.pdf) |
| tusd hooks can reject an upload; 15 s hook timeout | **Read in the docs** | [tusd hooks](https://tus.github.io/tusd/advanced-topics/hooks/), [tus protocol](https://tus.io/protocols/resumable-upload) |
| M920q storage limits and the 2.5" versus PCIe card trade-off | Lenovo spec sheet and a review, worth re-checking before buying | [Lenovo PSREF](https://psref.lenovo.com/syspool/Sys/PDF/ThinkCentre/ThinkCentre_M920_Tiny/ThinkCentre_M920_Tiny_Spec.PDF), [ServeTheHome](https://www.servethehome.com/lenovo-thinkcentre-m920-and-m920q-tiny-guide-and-review/) |
| Pi 5 PCIe 2.0 x1, two USB 3.0 ports, gigabit Ethernet, 27 W supply, 600 mA USB default | Product page and power guides | [Raspberry Pi 5](https://www.raspberrypi.com/products/raspberry-pi-5/), [powering the Pi 5](https://bret.dk/how-to-power-the-raspberry-pi-5-a-complete-guide/), [Pi forum](https://forums.raspberrypi.com/viewtopic.php?t=383340) |
| Samba speeds of 50 to 80 MB/s (hard disk) and about 110 MB/s (Pi 5 with SSD) | **Community-reported**, not lab-tested | [PiCockpit](https://picockpit.com/raspberry-pi/turn-your-raspberry-pi-into-a-nas-with-samba/), [Zbotic](https://zbotic.in/raspberry-pi-file-server-samba-nas-for-home-network/) |
| Camera-native H.265 at 100 to 200 Mb/s | **My estimate**, unverified | none |
| Whether a Pi can transcode proxies acceptably | **Not researched**: verify for your model | none |

A note on process: the first search summary I got claimed ProRes 422 HQ UHD 30p is 629 Mb/s and 283 GB per
hour. Reading Apple's table showed that was wrong (629 Mb/s is plain ProRes 422 at 4096×2160 30p). The
figures above are the verified ones.

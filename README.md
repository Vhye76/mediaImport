# mediaImport

An automatic media import pipeline in a container.  Drop a title into 'import/', collect it from 'complete/'.

It identifies the title against a provider, checks it against a minimum standard, compares it against whatever the library already holds, remuxes to Matroska, strips non-English tracks, writes the Matroska tag hierarchy, encodes, verifies the result, and puts the finished file where you can pick it up.

It never writes to a media library.  Moving finished titles in stays a manual step, on purpose.

## The chain

```
import/  ->  probe  ->  standards  ->  identify  ->  compare  ->  remux
         ->  tag  ->  readiness  ->  encode  ->  verify  ->  complete/
```

Assessment runs ahead of encoding.  MAX_JOBS workers take every title through probe, standards, identification, comparison and routing within minutes of a drop, so every gate failure is in the held queue long before the first encode finishes;  one thread per encoder, plus one for passthrough, then takes titles from their queues in order.  Anything that fails a gate goes to 'hold/' with a written reason and waits for a decision in the web UI.  A transient failure, such as a provider lookup that could not reach the network, holds with an exponential backoff and retries on its own:  five retries at 120, 240, 480, 960 and 1920 seconds, roughly 62 minutes in all, before it stops and waits for a person.

Nothing is ever deleted.  Sources are retired to 'complete/.quarantine' after the title completes.

## Stages

Every title carries a stage, shown in the Stage column of the dashboard.  These are the values you will see there and what each one means.

| Stage | Shown as | Meaning |
| --- | --- | --- |
| DETECTED | queued | Seen in 'import/', size stable across two polls and untouched for MTIME_QUIET seconds.  Waiting for a free worker.  A title returns here when you press Retry or Force through, and when a retry backoff expires. |
| PROBED | probed | One ffprobe pass done.  Classified as a movie or as television. |
| SCREENED | screened | Passed the minimum standards gate. |
| IDENTIFIED | identified | Provider IDs resolved and verified.  The canonical name is settled from here on. |
| COMPARED | compared | Checked against whatever the library already holds.  Also the value recorded when no library is mounted, when there is no incumbent, and when the incumbent could not be read. |
| ROUTED | waiting for encoder | The encoder is chosen and the title is queued for its pool:  CPU, GPU or passthrough.  Assessment is done;  everything from here runs on the pool's thread when one is free. |
| STAGED | copying | Copying the source into the encode work area.  A multi-gigabyte title sits here for minutes. |
| REMUXED | remuxed | Converted to Matroska if needed, non-English tracks dropped, track flags corrected. |
| TAGGED | tagged | Matroska tag block and segment title written. |
| READY | ready | Passed the readiness gate, or was forced past it, and is queued for an encoder slot. |
| ENCODING | encoding | An encoder is running.  Percent complete, estimated time remaining and speed appear beside it. |
| ENCODED | encoded | The encoder finished, or the router chose passthrough and no re-encode was needed. |
| VERIFIED | verified | Duration, packet count, track statistics, tag structure and HDR declarations checked on the finished file, every failure collected before it holds. |
| PUBLISHED | ready to promote | **The file is in 'complete/' and is yours to collect.**  This is the end of the pipeline as far as you are concerned. |
| CLEANUP | ready to promote | Housekeeping after publishing:  the source is retired to quarantine and the work area is wiped.  It touches nothing you collect, so it reads the same as PUBLISHED. |

Three further values sit outside the pipeline.

| Stage | Shown as | Meaning |
| --- | --- | --- |
| HELD | needs a decision | A gate failed and the title is waiting for you.  Every reason is listed, not only the first:  the standards, identification and comparison checks all run before a title holds, so one Force through is an informed decision rather than a guess repeated until the title moves.  The decision queue offers Retry, Force through and Discard. |
| QUARANTINED | rejected | Refused, or beaten by the library incumbent.  The file is in 'complete/.quarantine'. |
| FAILED | failed | A mechanical failure:  an unreadable probe, a remux or encoder that exited non-zero, or a publish that could not write.  Nothing was moved or deleted, and no output exists, so Force through cannot apply and is not offered;  Retry and Discard are. |

A row marked "files gone" refers to a title whose files you have since removed by hand.  It is a record of what the pipeline did rather than something still on disk, and the Forget button removes the record.  Forget only removes a database row;  it never deletes a file.

## Layout

```
app/            the pipeline: one module per concern
  main.py         supervisor, startup, signals
  config.py       the environment interface
  paths.py        mount contract, write guards, atomic publish
  locks.py        single-instance lock and encode job ownership
  orchestrator.py the state machine, the assessment workers and the encoder pools
  encode.py       the encoder router and command builders
  gpu.py          the runtime GPU probe
  media.py        remux, language strip, flag repair, cropdetect, grain probe
  tags.py         Matroska tag hierarchy and the readiness gate
  probe.py        one ffprobe pass, the attribute set everything else consumes
  standards.py    the minimum-standards gate
  compare.py      new versus incumbent
  titles.py       the filename transform and naming rules
  episodes.py     episode matching, ranges, part markers
  provider.py     Wikidata, TMDB and TVDB lookups
  state.py        SQLite store, one row per title
  webui.py        JSON API and dashboard
  audit.py        the background library sweep
  static/         the dashboard page
Dockerfile      alpine:3.24 plus ffmpeg, mkvtoolnix and the Intel media stack
entrypoint.sh   drops to PUID/PGID, joins RENDER_GID for /dev/dri
TESTPLAN.md     container validation cases, executed by hand
media/          the container icon, a placeholder
```

## Mount contract

The pipeline is import, then encode, then complete.  One mount is required and everything else derives from it.  'import', 'complete', 'complete/.quarantine' and 'hold' are always subdirectories of the root and are not configurable, because a move between them is then a rename rather than a copy.

| Container path | Env var | Mode | Required | Purpose |
|---|---|---|---|---|
| /media | MEDIA_ROOT | rw | yes | the one required mount, everything derives from it |
| /media/encode | MEDIA_ENCODE | rw | no | per-title work area, mount separately for fast storage |
| /media/config | MEDIA_CONFIG | rw | no | state.db, instance lock, provider cache, logs |
| /media/library/movies | LIBRARY_MOVIES | ro | no | incumbent comparison |
| /media/library/tv | LIBRARY_TV | ro | no | incumbent comparison |
| /certs | CERT_DIR | ro | yes | TLS certificate and key |

Quarantine is not a mount.  Retired sources and rejected files go to '/media/complete/.quarantine'.

Without a library mount the incumbent comparison is skipped and every title is treated as new, which is logged at startup and shown in the UI.  A missing root is a startup error naming the variable.

The whole per-title work area lives on the encode mount, not just the encode.  A title is copied in once, then remuxed, tagged, encoded and verified there, and the finished file is moved out once.  That is two crossings of the slow filesystem in exchange for keeping three or four full-file rewrites on fast storage.  Startup compares the filesystem of the encode and complete mounts and warns when they match, so a fast disk that silently landed on the same filesystem is visible rather than mysterious.

## Naming

The Matroska tag carries the provider's title verbatim, character for character, punctuation and all.  The filename is that same title with only the unsafe characters removed, plus the control range:

```
/   \   :   *   ?   "   <   >   |
```

Nothing is re-worded, abbreviated, reordered or truncated.  Those nine are what SMB forbids, and the libraries are served over SMB, so SMB is the binding constraint rather than the filesystem.

A colon is REMOVED rather than turned into a dash, so 'Avengers: Endgame' is filed as 'Avengers Endgame'.  An em dash or en dash becomes ' - ', and a forward slash inside a title becomes '-'.  Everything else is kept, including parentheses, brackets, commas, apostrophes, ampersands and accented letters.  The parentheses and brackets are structural:  '(Year)' appears in every movie name, and '[tmdbid-N]', '[imdbid-ttN]' and '[tvdbid-N]' are what the incumbent lookup keys on.

```
Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv
Show Name (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show Name - SNNENN - Episode Title.mkv
```

A file covering two episodes takes the range form, 'Show - S05E01-E02 - Kidnapping.mkv', because naming it as only the first makes Jellyfin report the second as missing.  A multi-part episode uses ', Part 1' rather than whichever marker the provider happened to use.

The longer form that repeats the whole folder name inside the filename is for editions and nothing else:

```
Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644]/Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644] - Assembly Cut.mkv
```

The transform runs one way.  A filename can always be derived from a tag;  a tag can never be derived from a filename, because the information needed has already been discarded.  Where a tag and a filename differ by unsafe characters alone, that is expected and is not a defect.

## Environment

| Name | Default | Purpose |
|---|---|---|
| MEDIA_ROOT | /media | the one mount everything derives from;  defaulted rather than demanded, then validated to be a mounted directory |
| MEDIA_ENCODE | <root>/encode | optional, per-title work area on faster storage |
| MEDIA_CONFIG | <root>/config | optional, state.db, lock, cache, logs |
| LIBRARY_MOVIES | unset | ro movie library |
| LIBRARY_TV | unset | ro tv library |
| CERT_DIR | /certs | ro, holds the TLS certificate and key |
| TLS_CERT_FILE | fullchain.pem | certificate name within CERT_DIR |
| TLS_KEY_FILE | privkey.pem | private key name within CERT_DIR |
| PUID / PGID | required | identity the supervisor drops to;  enforced by 'entrypoint.sh', which refuses to start without both |
| RENDER_GID | unset | supplementary group for /dev/dri, GPU off if unset |
| RENDER_NODE | /dev/dri/renderD128 | render node the GPU probe and QSV encoder use |
| OUTPUT_CODEC | hevc | hevc or av1 |
| MAX_JOBS | 3 | assessment workers:  probe, screen, identify, compare and route, ahead of any encode |
| GPU_SLOTS | 1 | GPU encode threads |
| CPU_SLOTS | 1 | CPU encode threads, each at ENCODE_THREADS divided by CPU_SLOTS;  two gain little except on SD |
| ENCODE_HEADROOM | 3.0 | multiple of source size required to admit a job |
| ENCODE_THREADS | 0 | 0 autodetects from the cgroup CPU quota |
| CRF | 18 | default quality target |
| TV_ENCODE_SD | 0 | 1 re-enables SD television encoding |
| WEB_PORT | 443 | HTTPS only, there is no HTTP listener |
| DRY_RUN | 0 | 1 logs every intended action and performs none |
| POLL_INTERVAL | 15 | import watch interval in seconds |
| MTIME_QUIET | 30 | seconds a file must be untouched before it counts as stable.  Keep POLL_INTERVAL below this |
| LOCK_WAIT_TIMEOUT | 0 | seconds to wait for the instance lock, 0 waits indefinitely |
| LOCK_WAIT_INTERVAL | 15 | how often to retry the instance lock |
| GRAIN_THRESHOLD | 0.18 | denoise delta above which a source counts as grainy |
| LOG_LEVEL | info | 'info' records what happened, 'debug' adds why |
| AUDIT_INTERVAL | 2 | seconds between library files audited;  0 disables the sweep |
| AUDIT_SWEEP_INTERVAL | 3600 | seconds between passes over the libraries |

Every one of these is echoed into the log and onto /api/status at startup, so what the container thinks it was configured with is always visible without exec-ing into it.

Every module logs what it does.  At the default 'info' the log records one line per meaningful action, naming the title, the stage and the outcome, and says why when something fails or degrades.  Set 'LOG_LEVEL=debug' to add the detail behind each of those lines:  command lines, per-gate comparisons, measured figures against their thresholds.  Logs go to stdout and to 'config/logs/mediaimport.log', and the tail is served at /api/logs.

Six more are read directly by the modules that use them and are neither validated nor reported.  They exist to substitute a binary, not to configure the service:  FFPROBE, FFMPEG, MKVMERGE, MKVPROPEDIT, MKVEXTRACT and VAINFO.

Two host-side values are consumed by the compose file rather than the container, and set the kernel cgroup limits:

| Name | Default | Purpose |
|---|---|---|
| CONTAINER_CPUS | 8 | CPU quota;  ENCODE_THREADS derives the encoder thread count from it |
| CONTAINER_MEM | 16g | memory ceiling;  nothing derives from it, it just has to be enough |

Get RENDER_GID from the host that will run the container:

```
stat -c %g /dev/dri/renderD128
```

## The encoder router

Evaluated in order, first match wins.

```
1  source codec is hevc or av1          PASSTHROUGH
2  television and the source is SD      PASSTHROUGH
3  Dolby Vision RPU present             libx265    on any setting
4  OUTPUT_CODEC=av1 and grainy          libsvtav1  CPU
5  OUTPUT_CODEC=av1                     av1_qsv    GPU
6  grainy                               libx265 aq-mode=4:tune=grain
7  otherwise                            libx265 aq-mode=3
```

x265 is the default because no Apple TV decodes AV1 in hardware.  AV1 is fully built and selectable with OUTPUT_CODEC=av1, but the calibration batch has not been run, so the AV1 rate control values are starting points rather than settled ones.

Gate 3 exists because an AV1 re-encode discards the Dolby Vision RPU.  Those titles always take the x265 path, on any setting.

An HDR source that reaches an encoder carries its colour, mastering display and content light level into the x265 params, and a Dolby Vision source carries its RPU through with ffmpeg's native '-dolbyvision' and a VBV pair, no extraction tool involved.  In practice neither happens:  HDR and Dolby Vision material is HEVC, and gate 1 passes it through untouched.

Gate 5 is the one that can produce an encoder the table does not name.  At startup 'vainfo' must report VAProfileAV1Profile0 with the encode entrypoint;  a failed probe does not crash the container, it marks the GPU degraded, and gate 5 then routes to libsvtav1 on the CPU instead of av1_qsv.  At the hevc default a degraded GPU changes nothing at all, which is the point:  the GPU cannot break the pipeline.  Every encode logs which encoder actually ran, so a GPU that has quietly stopped being used is visible rather than silent.

SD is decided on display height, computed from width times SAR over height, so an anamorphic PAL DVD rip is classified on what it actually displays rather than on its stored dimensions.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE.  It does not mean no processing.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given track statistics.  An SD AVI rip arriving in 'complete/' still as an .avi would be a bug.

Grain is detected automatically, by encoding a 20 second sample twice, once clean and once through a light denoise, and comparing the two sizes.  The ratio is logged for every title so a bad threshold is visible rather than silent.  An 'encode.job' sidecar overrides it.  It is read from the directory holding the source rather than from a per-title path, so one file governs every source alongside it:  convenient for a season, surprising for a mixed drop.

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
```

## Comparison gates

Before any encode, an arrival is compared against whatever the library already holds.  Seven gates, in order:

```
1  HDR or Dolby Vision present    losing it is never an upgrade
2  display pixel count            width * SAR / height, never stored dimensions
3  baked-in letterbox             larger real picture area wins
4  audio maximum channel count    5.1 beats 2.0
5  bit depth                      10-bit beats 8-bit
6  video bitrate                  weighted for codec efficiency
7  source pedigree                remux > encode > web, tiebreak only
```

EVERY GATE IS EVALUATED AND THE VOTES ARE TALLIED.  The first difference does not decide.  Every vote a win and the title proceeds to the encode.  Every vote a loss and it goes to quarantine with no encode spent on it.  Votes in both directions go to hold, behind the Compare button, and so does a pair on which no gate voted at all.  Nothing resolves a split verdict automatically, because there is no correct automatic answer for a file that is better in one respect and worse in another.  The two exits from that hold are Force through and Discard, and both are yours.

Gate 1 is asymmetric and is the only short circuit.  An arrival that LACKS HDR or Dolby Vision the incumbent carries is an immediate loss and no further gate runs.  An arrival that GAINS it casts an ordinary win vote and can be contradicted into review.

Gate 2 defers to gate 3 whenever either side carries baked-in bars, because display pixel count counts black as picture.  A correctly cropped 1920x800 arrival and an incumbent stored 1920x1080 with 280 px of bars hold identical real picture, and gate 2 on its own would decide that on a margin that is entirely black.

Gate 6 is comparative only.  There is no minimum bitrate anywhere in this pipeline and none is to be added.  Raw figures are weighted by codec first, h264 at 1.0, HEVC at 1.7 and AV1 at 2.2, because without the weighting the gate systematically favours the less efficient codec:  a surviving h264 source would rate above the HEVC this pipeline produced from it, and the pipeline would quarantine its own output.

Gate 7 is a tiebreak rather than a vote.  Pedigree is inferred from release naming, which is exactly the kind of signal the rest of this pipeline distrusts, so it is consulted only when no measurable gate voted and it is marked as a weak signal when it decides.

Gates 2, 3 and 6 need both sides to be measurable.  Where one side is missing, the gate casts no vote and the skip is recorded rather than counted as a tie.  The tolerances are 5 percent on pixel count and 25 percent on bitrate;  the bitrate figure is a starting point and has not been calibrated against this library.

The table behind the Compare button carries every attribute the pipeline measured, not only the seven it gates on.  A row that differs with no gate against it is marked as such, and that is the row worth looking at:  it is where the pipeline saw a difference and had no rule for it.

The incumbent is read, compared against and left alone.  Nothing in the container writes to a library.

HDR is compared on presence at gate 1, and on declaration in the table.  A Matroska file states its mastering display and content light level twice, in the bitstream as SEI and in the container's Colour element, and the two can disagree:  eight HDR titles in one library all carried the metadata in the bitstream while three declared none of it in the container.  The pipeline probes both surfaces, repairs a container that under-declares its own bitstream with a header edit on the way through, and holds any title whose output declares less than its source carried.  A content light level of zero and zero, an encoder's way of saying not indicated, is written like any other and read back through mkvmerge, because ffprobe reports a Matroska content light element only when both values are non-zero.  The declaration rows appear in the Compare table without a gate number, so a difference there is one of the marked rows worth looking at rather than a vote.

## Identification and the internet

A provider ID is never guessed.  Resolution goes through Wikidata and then verifies against the TMDB or TVDB page before an ID is written anywhere, because Wikidata's provider IDs can be flat wrong.  Movies use tmdbid and imdbid;  television uses tvdbid and tmdbid, since TVDB governs episode titles and numbering.  Requests are spaced about three seconds apart, and every answer is cached on disk under 'config/cache', which is consulted before any request is made.

A file that has already been through this pipeline, or that came back out of a library, states what it is:  embedded tags, then ids in the filename, then ids in the folder, then ids in the library folder a repair copy came from, then the segment title are all tried before the cleaned filename is.  Television runs the same ladder in the same shape, with the COLLECTION block, the show folder and the origin folder ahead of the show name.  A fresh disc rip has none of those, so for that case the filename is all there is.

An id found on any of those rungs is a pointer, not an identity.  It is looked up on Wikidata, and the entity supplies the title, the year and the other id;  the TMDB or TVDB page is then checked by its own title and year against the entity's label and aliases.  Nothing on disk becomes a tag:  a folder written before the naming rules changed, or a tag block written by an earlier tool in filename form, is corrected to the provider's title on the way through.  An identity that is still missing a field holds with the field named rather than publishing a folder with 'None' in it.

A filename has already lost the provider's punctuation, and Wikidata's prefix search stops at a colon, so a search is matched under the naming rules rather than by string:  every candidate's label is put through the same transform the filename went through, a full-text search covers the entities the prefix search cannot reach, and a candidate whose release year is more than a year from the name's is skipped.  'Star Wars Episode IV A New Hope' and 'Futurama Bender's Game' both resolve from their filename form.

Episodes are matched by title against the provider's list and the SNNENN is derived from the match, never read out of the source filename.  Release groups renumber when they collapse a two-part episode into one file, and everything after it silently shifts.  A fuzzy fallback covers the typos scene filenames carry.  A file matching neither exactly nor fuzzily falls back to source numbering with a warning, and a title that cannot be identified at all holds.

Every answer is cached without expiry, an empty search result included, so a title held for an unresolvable name would hold again identically on any requeue.  Retry on a held title therefore asks the providers again, bypassing the cache for that one identification;  Force through does not, and carries the title on without an ID.

THERE IS NO OFFLINE MODE, and this is the one that reads as a hang.  A container with no outbound access cannot identify anything, so every title holds on the backoff described above and then waits for a person.  That is the intended behaviour rather than a fault, but it is worth knowing before pointing this at an isolated network.

A title that cannot be identified holds, and Force through carries it on without an ID rather than guessing one.  A forced unidentified title keeps the name it arrived with, extension changed to '.mkv', carries a tag block holding TITLE only, and lands flat in 'complete/' instead of in a provider-named folder, so it is visibly unlike finished work.

Cover art is a by-product of the same lookup.  The poster comes off the TMDB page already fetched to verify the title, with TVDB as the fallback for a show that resolved without a TMDB id, and there is no API key involved.  Posters are cached under 'config/cache/posters' and served from '/api/poster/<hash>', keyed on the artwork URL rather than the title so a long browser cache header stays honest across a store wipe, and the browser never contacts an image CDN and the dashboard renders on a LAN with no internet once a poster is cached.  A fetch that fails serves 404 and the tile falls back to text;  artwork is never allowed to become a failure the pipeline notices.

## Library audit

A background sweep over the mounted libraries, looking for every deviation the passthrough path already corrects and nothing that needs an encode:  a container that is not Matroska, foreign tracks, wrong default flags, a missing or flattened tag block, a folder or file name that differs from what the tag block would produce, a path component that breaks a naming rule, a wrong segment title, missing statistics, and an HDR declaration short of the bitstream.  The names are built from the tag block by the same functions the publish step uses, so a folder that predates the current transform, a show folder carrying the wrong ids, an unpadded season folder and a mis-numbered file are all findings;  a tag that is itself wrong, with names that agree with it, is not, because the audit never consults a provider.  Resolution, bit depth, codec and letterbox are never findings.

It is throttled at AUDIT_INTERVAL seconds per file and skips files whose size and modification time it has already seen, so a first pass over a few thousand files takes a couple of hours and a repeat pass takes seconds.  That skip is what keeps the hourly pass cheap, and it also means a change to the checks never reaches a file that has not changed on disk:  'Rescan entire library' in the findings dialog wipes the findings and runs a first pass again.  It never starts when no library is mounted.

Repair is by running the file through the pipeline.  Each finding carries an Import action that copies the library file into 'import/', after which the ordinary chain remuxes, strips, repairs, tags and verifies it and leaves the result in 'complete/' for you to move into the library by hand.  A copied title skips the comparison against the file it came from and nothing else.  The copy refuses when the root lacks the space, when the name is already in 'import/', or while a title for that file is in the pipeline, which includes a published copy you have not yet moved into the library.  The copy runs in the background with a progress bar under the finding's buttons, and once it is in 'import/' the button reads In Pipeline, clickable through to the title once the watcher has picked it up, until the repaired file is in the library and the next audit pass clears the finding.

## One instance at a time

The container takes an exclusive 'flock' on MEDIA_CONFIG/mediaimport.lock at startup.  A second instance pointed at the same mounts waits for the first to exit rather than running alongside it, because two instances would sweep each other's encode area and could publish the same title twice.  The kernel releases the lock if the holder is killed, so a hard kill needs no manual cleanup.

Encode job directories record their owning PID.  The startup sweep reclaims only directories whose owner is gone, and leaves a live job alone.

## Web UI

HTTPS only, on WEB_PORT, which defaults to 443.  There is no HTTP listener and no redirect.  The certificate and key come from the '/certs' mount and are never generated:  if they are missing or unreadable the container logs the reason and exits rather than starting without TLS.

No authentication.  Anyone who can reach the port can drive it, including forcing a held title through and quarantining an incoming file, so publish the port deliberately.  TLS protects the traffic in transit;  it does not restrict who can use the API.

```
GET  /                            dashboard
GET  /api/status                  config, GPU state, encode space, stage counts, the depth of
                                  the assessment queue and each encoder pool's queue, active
                                  threads per pool, uptime, audit status, and live percent,
                                  ETA and speed for every running encode
GET  /api/titles                  every title
GET  /api/titles/<id>             one title with its stage history and comparison table
GET  /api/held                    the decision queue, held and failed titles together
GET  /api/poster/<hash>           cached cover art by the 'poster' hash on a title, 404 when there is none
GET  /api/logs                    log tail
GET  /api/audit                   sweep status and every library finding
POST /api/audit                   start a sweep now;  {"rescan": true} wipes the findings first
POST /api/audit/<id>/import       copy that finding's file into import/ for repair, in the background
POST /api/held/<id>/decision      {"action": "retry" | "override" | "discard" | "forget"}
```

A strip along the top carries the output codec, the GPU state, free space in the encode area, whether a library is mounted and a DRY_RUN badge, so a degraded GPU or an unmounted library is visible without opening anything.

The dashboard is a pipeline rather than a table.  Queue on the left, Encoding and Held as the two parallel paths out of it, Ready to promote on the right, and counters in the lower right:  library findings, with a scanning line beneath it while a pass runs, then quarantined files and failed jobs.  Each title is a cover art tile;  a title that has not been identified yet, or that was held before identification, shows its filename on the same footprint instead.  A season of television collapses to one tile per show with an episode count, and clicking it lists the episodes.

Clicking any tile opens its detail:  stage, provider ids, every reason it stopped where it did, both paths, and the full stage history.  A held title's detail carries Retry, Force through and Discard;  a failed title's carries Retry and Discard, with a line saying why Force cannot apply.  The three counters are clickable and list what is in them:  library findings, quarantined files and failed jobs, the first with a Details table per file, an Import action per row, and Sweep now and Rescan entire library in its header.

A television tile opens the episode list instead.  Every row carries the same decisions as a tile, and the header carries them for the whole season at once, so clearing a held season is one action rather than one per episode.  A season action closes the list, because there is nothing left in it to show.

Where a title was compared against a library incumbent, the detail shows a Compare button.  It opens a table of every attribute the pipeline measured on both files, side by side, with the published output as a third column once it exists.  Rows that a comparison gate acted on carry their gate number, every row whose gate cast a vote is marked, and a row that differs without any gate acting on it is marked too:  that is the case worth looking at, because the pipeline saw a difference and had no rule for it.

## Build and validate

While writing code, validate the code itself:

```
python3 -m compileall -q app
python3 -c "import app.main"
```

That is the whole of local validation.  Neither command executes a pipeline stage, touches a file or opens a socket.  There is no local test suite:  a workstation and this container are different environments, so functionality is validated in the container and nowhere else.

```
docker build -t mediaimport:local .
```

The image build fails if ffmpeg lacks libx265, libsvtav1 or av1_qsv, or if its libx265 wrapper has no '-dolbyvision' option.  Those checks are deliberate:  they stop the image shipping while claiming encoders or capabilities it does not have.  If one ever fails, change where ffmpeg comes from rather than deleting the check.  The escalation order is av1_vaapi, then a pinned ffmpeg from Alpine's edge community repository.

The image is Alpine 3.24, 282 MB, everything from Alpine's own repositories.  Every build increments the version in 'app/__init__.py';  the workflow refuses a version that is already tagged.

Functionality is validated against the built container by hand, following 'TESTPLAN.md'.  That plan measures outcome:  files, filenames, tag blocks, track lists, API responses, exit codes and health state.

Run it against a throwaway tree first:

```
docker run --rm -e PUID=1000 -e PGID=1000 -e DRY_RUN=1 \
  -e POLL_INTERVAL=15 -e MTIME_QUIET=30 -e LOG_LEVEL=info \
  -v /tmp/testtree:/media \
  -v /srv/certs:/certs:ro \
  -p 443:443 mediaimport:local
```

ONE ROOT MOUNT, DELIBERATELY.  'import', 'complete', 'complete/.quarantine' and 'hold' are created underneath it at startup, and a move between two of them is then a rename rather than a copy.  Mounting them individually turns every one of those moves into a copy at best;  at worst 'rename' refuses outright with EXDEV, because Linux will not rename across two mount points even when both sides are the same device.

DRY_RUN logs every intended move, encode and quarantine and performs none of them.

## Deploying

Copy '.env.example' to '.env', fill in the host paths, the certificate directory and RENDER_GID, then:

```
docker compose up -d
```

CI does not build on push.  The workflow is manual only, started from the Actions tab, so a commit or a tag publishes nothing on its own.

## Version

Current version 0.7.0, defined once in 'app/__init__.py' and consumed by the provider User-Agent, the startup log and the image tag.  Every build increments it;  the workflow refuses a version that is already tagged.

'x.0.0' is a release, '0.x.0' is a minor update or bug fix, and '0.0.x' is a pre-release.  Tags are bare numeric, with no 'v' prefix.  A tag records a point in history;  it does not trigger a build.

## Licence

GPL-3.0.  See LICENSE.

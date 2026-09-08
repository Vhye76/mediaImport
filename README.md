# mediaImport

An automatic media import pipeline in a container.  Drop a title into 'import/', collect it from 'complete/'.

It identifies the title against a provider, checks it against a minimum standard, compares it against whatever the library already holds, remuxes to Matroska, strips non-English tracks, writes the Matroska tag hierarchy, encodes, verifies the result, and puts the finished file where you can pick it up.

It never writes to a media library.  Moving finished titles in stays a manual step, on purpose.

## The chain

```
import/  ->  probe  ->  standards  ->  identify  ->  compare  ->  remux
         ->  tag  ->  readiness  ->  encode  ->  verify  ->  complete/
```

Anything that fails a gate goes to 'hold/' with a written reason and waits for a decision in the web UI.  A transient failure, such as a provider lookup that could not reach the network, holds with an exponential backoff and retries on its own before it stops and waits for a person.

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
| STAGED | copying | Copying the source into the encode work area.  A multi-gigabyte title sits here for minutes. |
| REMUXED | remuxed | Converted to Matroska if needed, non-English tracks dropped, track flags corrected. |
| TAGGED | tagged | Matroska tag block and segment title written. |
| READY | ready | Passed the readiness gate and is queued for an encoder slot. |
| ENCODING | encoding | An encoder is running.  Percent complete, estimated time remaining and speed appear beside it. |
| ENCODED | encoded | The encoder finished, or the router chose passthrough and no re-encode was needed. |
| VERIFIED | verified | Duration, track statistics and tag structure checked on the finished file. |
| PUBLISHED | ready to promote | **The file is in 'complete/' and is yours to collect.**  This is the end of the pipeline as far as you are concerned. |
| CLEANUP | ready to promote | Housekeeping after publishing:  the source is retired to quarantine and the work area is wiped.  It touches nothing you collect, so it reads the same as PUBLISHED. |

Three further values sit outside the pipeline.

| Stage | Shown as | Meaning |
| --- | --- | --- |
| HELD | needs a decision | A gate failed and the title is waiting for you.  The reason is written out, and the decision queue offers Retry, Force through and Discard. |
| QUARANTINED | rejected | Refused, or beaten by the library incumbent.  The file is in 'complete/.quarantine'. |
| FAILED | failed | An unexpected error.  Nothing was moved or deleted. |

A row marked "files gone" refers to a title whose files you have since removed by hand.  It is a record of what the pipeline did rather than something still on disk, and the Forget button removes the record.  Forget only removes a database row;  it never deletes a file.

## Layout

```
app/            the pipeline: one module per concern
  main.py         supervisor, startup, signals
  config.py       the environment interface
  paths.py        mount contract, write guards, atomic publish
  locks.py        single-instance lock and encode job ownership
  orchestrator.py the state machine and the two-slot encode scheduler
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
  static/         the dashboard page
Dockerfile      debian:trixie-slim plus ffmpeg, mkvtoolnix and the Intel media stack
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

## Environment

| Name | Default | Purpose |
|---|---|---|
| MEDIA_ROOT | /media | required rw, the one mount everything derives from |
| MEDIA_ENCODE | <root>/encode | optional, per-title work area on faster storage |
| MEDIA_CONFIG | <root>/config | optional, state.db, lock, cache, logs |
| LIBRARY_MOVIES | unset | ro movie library |
| LIBRARY_TV | unset | ro tv library |
| CERT_DIR | /certs | ro, holds the TLS certificate and key |
| TLS_CERT_FILE | fullchain.pem | certificate name within CERT_DIR |
| TLS_KEY_FILE | privkey.pem | private key name within CERT_DIR |
| PUID / PGID | required | identity the supervisor drops to |
| RENDER_GID | unset | supplementary group for /dev/dri, GPU off if unset |
| RENDER_NODE | /dev/dri/renderD128 | render node the GPU probe and QSV encoder use |
| OUTPUT_CODEC | hevc | hevc or av1 |
| MAX_JOBS | 3 | concurrent titles, also bounded by encode free space |
| GPU_SLOTS | 1 | concurrent GPU encodes |
| CPU_SLOTS | 1 | concurrent CPU encodes |
| ENCODE_HEADROOM | 3.0 | multiple of source size required to admit a job |
| ENCODE_THREADS | 0 | 0 autodetects from the cgroup CPU quota |
| CRF | 18 | default quality target |
| TV_ENCODE_SD | 0 | 1 re-enables SD television encoding |
| WEB_PORT | 443 | HTTPS only, there is no HTTP listener |
| DRY_RUN | 0 | 1 logs every intended action and performs none |
| POLL_INTERVAL | 60 | import watch interval in seconds |
| MTIME_QUIET | 120 | seconds a file must be untouched before it counts as stable |
| LOCK_WAIT_TIMEOUT | 0 | seconds to wait for the instance lock, 0 waits indefinitely |
| LOCK_WAIT_INTERVAL | 15 | how often to retry the instance lock |
| GRAIN_THRESHOLD | 0.18 | denoise delta above which a source counts as grainy |
| LOG_LEVEL | info | 'info' records what happened, 'debug' adds why |

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

SD is decided on display height, computed from width times SAR over height, so an anamorphic PAL DVD rip is classified on what it actually displays rather than on its stored dimensions.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE.  It does not mean no processing.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given track statistics.  An SD AVI rip arriving in 'complete/' still as an .avi would be a bug.

Grain is detected automatically, by encoding a 20 second sample twice, once clean and once through a light denoise, and comparing the two sizes.  The ratio is logged for every title so a bad threshold is visible rather than silent.  An 'encode.job' sidecar beside the source overrides it:

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
```

## One instance at a time

The container takes an exclusive 'flock' on MEDIA_CONFIG/mediaimport.lock at startup.  A second instance pointed at the same mounts waits for the first to exit rather than running alongside it, because two instances would sweep each other's encode area and could publish the same title twice.  The kernel releases the lock if the holder is killed, so a hard kill needs no manual cleanup.

Encode job directories record their owning PID.  The startup sweep reclaims only directories whose owner is gone, and leaves a live job alone.

## Web UI

HTTPS only, on WEB_PORT, which defaults to 443.  There is no HTTP listener and no redirect.  The certificate and key come from the '/certs' mount and are never generated:  if they are missing or unreadable the container logs the reason and exits rather than starting without TLS.

No authentication.  Anyone who can reach the port can drive it, including forcing a held title through and quarantining an incoming file, so publish the port deliberately.  TLS protects the traffic in transit;  it does not restrict who can use the API.

```
GET  /                            dashboard
GET  /api/status                  config, GPU state, encode space, queue depth, stage counts
GET  /api/titles                  every title
GET  /api/titles/<id>             one title with its stage history and comparison table
GET  /api/held                    the decision queue
GET  /api/logs                    log tail
POST /api/held/<id>/decision      {"action": "retry" | "override" | "discard" | "forget"}
```

Where a title was compared against a library incumbent, the dashboard shows a Compare button.  It opens a table of every attribute the pipeline measured on both files, side by side, with the published output as a third column once it exists.  Rows that a comparison gate acted on carry their gate number, the row that decided the outcome is marked, and a row that differs without any gate acting on it is marked too:  that is the case worth looking at, because the pipeline saw a difference and had no rule for it.

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

The image build fails if ffmpeg lacks libx265, libsvtav1 or av1_qsv.  That check is deliberate:  it stops the image shipping while claiming encoders it does not have.  If it ever fails, change where ffmpeg comes from rather than deleting the check.  The escalation order is av1_vaapi, then jellyfin-ffmpeg from the Jellyfin apt repository.

Functionality is validated against the built container by hand, following 'TESTPLAN.md'.  That plan measures outcome:  files, filenames, tag blocks, track lists, API responses, exit codes and health state.

Run it against a throwaway tree first:

```
docker run --rm -e PUID=1000 -e PGID=1000 -e DRY_RUN=1 \
  -v /tmp/testtree/import:/media/import \
  -v /tmp/testtree/encode:/media/encode \
  -v /tmp/testtree/complete:/media/complete \
  -v /tmp/testtree/hold:/media/hold \
  -v /tmp/testtree/config:/media/config \
  -v /srv/certs:/certs:ro \
  -p 443:443 mediaimport:local
```

DRY_RUN logs every intended move, encode and quarantine and performs none of them.

## Deploying

Copy '.env.example' to '.env', fill in the host paths, the certificate directory and RENDER_GID, then:

```
docker compose up -d
```

CI does not build on push.  The workflow is manual only, started from the Actions tab, so a commit or a tag publishes nothing on its own.

## Version

Current version 0.0.6, defined once in 'app/__init__.py' and consumed by the provider User-Agent and the startup log.

'x.0.0' is a release, '0.x.0' is a minor update or bug fix, and '0.0.x' is a pre-release.  Tags are bare numeric, with no 'v' prefix.  A tag records a point in history;  it does not trigger a build.

## Licence

GPL-3.0.  See LICENSE.

# mediaImport - operating ruleset for this repository

## 1.  What this file is

THIS FILE IS THE SOURCE OF TRUTH FOR THIS REPOSITORY.  It is the operating ruleset for the container:  what it must do, what it must never do, and why each rule exists.

It is scoped deliberately.  The full media library standards, covering the workstation scripts, the NAS layout, library-wide audits and the music library, live in a separate CLAUDE.md alongside those tools.  Where a rule here is derived from that document it is restated in full rather than cross-referenced, because this repository has to be readable on its own.

Rules here carry the measurement or the incident that produced them.  That is not decoration.  A rule without its evidence gets "simplified" by the next person who reads it, and every one of these was written after something went wrong.

## 2.  Hard boundaries

THE CONTAINER NEVER WRITES TO A MEDIA LIBRARY.  Libraries are mounted read only at the container boundary, which is a stronger guarantee than any check in Python.  The guards in 'app/paths.py' stay anyway as defence in depth.

PROMOTION INTO THE LIBRARIES IS MANUAL, ALWAYS.  The pipeline ends at 'complete/'.  No code in this repository moves a finished title into a library, and none should be added.

NOTHING THAT MATTERS IS EVER DELETED.  A failure holds.  A rejected file goes to quarantine.  A completed source is retired to quarantine.  Nothing in a library, nothing incoming and nothing in 'complete/' is ever removed, and no code doing so should be added.  The exception, and it is the only one, is the encode area:  intermediates there are removed once their successor exists, and a job directory is wiped after its title retires.  Those are the nine deletion calls in the codebase and they touch nothing else.

A PROVIDER ID IS NEVER GUESSED.  If it cannot be resolved, the title holds.  Guessed numeric IDs have historically returned a Russian district, a Polish village, a bank and an unrelated film, each confidently formatted and entirely wrong.

## 3.  Repository layout

```
app/
  main.py           supervisor: config, logging, lock, signals, startup order
  config.py         the environment interface, validated once at load
  paths.py          mount contract, write guards, atomic publish, encode job dirs
  locks.py          single-instance flock, encode job ownership, PID liveness
  orchestrator.py   the state machine, the watcher, the two-slot encode scheduler
  encode.py         the encoder router and the three command builders
  gpu.py            the runtime GPU probe, vainfo parsing, degraded status
  media.py          remux, language strip, flag repair, cropdetect, grain probe
  tags.py           Matroska tag hierarchy, tag writing, the readiness gate
  probe.py          one ffprobe pass, the attribute set everything else consumes
  standards.py      the minimum-standards gate
  compare.py        new versus incumbent, six ordered gates
  titles.py         the filename transform and the naming rules
  episodes.py       episode matching, ranges, part markers, fuzzy fallback
  provider.py       Wikidata, TMDB and TVDB lookups with an on-disk cache
  state.py          SQLite store, one row per title, plus stage history
  webui.py          JSON API and dashboard
  static/           the dashboard page, vanilla JS, no framework
media/              the container icon, a placeholder, excluded from the image
Dockerfile          debian:trixie-slim plus ffmpeg, mkvtoolnix, Intel media stack
entrypoint.sh       drops to PUID/PGID, joins RENDER_GID for /dev/dri
TESTPLAN.md         container validation cases, executed by hand
```

'app/' is a single Python package with no third-party dependencies.  The standard library is sufficient and the HTTP client is hand-rolled on urllib.  KEEP IT THAT WAY.  No requests, no npm, no framework, no CDN.  A dependency-free image is trivially auditable and never breaks on a transitive upgrade.

One process holds everything:  the orchestrator loop, the encode workers and the web UI on a thread.  That is deliberate.  The UI needs live orchestrator state, and every encode is a subprocess call, so the GIL is not a constraint.

## 4.  Mount contract

ONE REQUIRED MOUNT.  Host paths are a deployment detail and appear only in the reference compose file.  Nothing in the image knows or cares what they are.

```
CONTAINER PATH          ENV VAR         MODE  REQUIRED  DEFAULT
/media                  MEDIA_ROOT      rw    yes       -
/media/encode           MEDIA_ENCODE    rw    no        <root>/encode
/media/config           MEDIA_CONFIG    rw    no        <root>/config
/media/library/movies   LIBRARY_MOVIES  ro    no        unset
/media/library/tv       LIBRARY_TV      ro    no        unset
/certs                  CERT_DIR        ro    yes       /certs
```

FOUR DIRECTORIES ARE DERIVED FROM THE ROOT AND ARE NOT CONFIGURABLE:

```
<root>/import
<root>/complete
<root>/complete/.quarantine
<root>/hold
```

THAT IS DELIBERATE AND IT IS LOAD BEARING.  A title moves between those four, and a move between two paths under one mount is a rename:  instant, no data copied, whatever the file size.  Split them onto separate mounts and every one of those moves becomes a copy at best, and at worst fails outright.  It failed outright once;  see section 21.

ONLY ENCODE AND CONFIG ARE OVERRIDABLE, because they are the two that benefit from faster storage and neither is a rename target.  Left unset they are subdirectories of the root and everything is one mount.  Pointed elsewhere, the crossing is paid once at publish, which is a copy either way when the encode area is on a different pool.

THE PIPELINE IS IMPORT, ENCODE, COMPLETE.  Those three are the stages.  'hold/' and 'config/' are not stages;  they are where a title goes when it cannot proceed, and where the machinery keeps its state.

```
import/       drop zone, the only watched entry point
encode/       the entire per-title work area, one directory per running job
complete/     TERMINAL, collected by hand
complete/.quarantine/   retired sources and rejected incoming files
hold/         needs a decision
config/       state.db, the instance lock, provider cache, logs
```

QUARANTINE LIVES UNDER 'complete/', not on its own mount.  It is a dotted directory so it sits beside finished work without being mistaken for it.  Nothing in the container ever scans 'complete/';  the orchestrator only joins paths to write into it, so a dotted sibling costs nothing.

A MISSING ROOT IS A STARTUP ERROR, not a directory quietly created in the wrong place.  'MEDIA_ENCODE' and 'MEDIA_CONFIG' are validated only when they are set explicitly;  unset, 'Layout.ensure' creates them under the root.  'config/' has to be persistent wherever it lands:  it holds the SQLite store, so losing it means re-processing everything, and it holds the flock that section 19's single-instance guarantee depends on, which only works if two containers can see the same file.

DEGRADE, DO NOT FAIL, APPLIES TO THE LIBRARIES ONLY.  Without a library mount the incumbent comparison is skipped and every title is treated as new.  Logged at startup and shown in the UI, because silently skipping a gate is worse than not having one.

THE WHOLE PER-TITLE WORK AREA LIVES ON THE ENCODE MOUNT, NOT JUST THE ENCODE ITSELF.  An earlier design kept a staging area on the array and used fast storage only for encoder output.  That optimised the one stage that does not need it:  an x265 encode is CPU bound, and reading the source sequentially is not what makes it slow.  The expensive I/O is on either side, and each pass rewrites the whole file:

```
mp4 to mkv or avi to mkv remux     full read + full write
language strip                     full read + full write
encode                             full read + full write
packet count verification          full read of the source and of the output
statistics and tag writes          header rewrites plus a full re-read on re-add
```

Three or four complete passes per title.  So:  copy in once, do everything on the encode mount, move out once.  Two crossings of the slow filesystem in exchange for keeping every intermediate rewrite on fast storage.  There is no staging directory on the array at all.

Startup compares 'os.stat().st_dev' of the encode mount against the complete mount and warns when they match, so a fast disk that silently landed on the same filesystem is visible rather than mysterious.

Admission control requires roughly ENCODE_HEADROOM times the source size free before a job starts, read at admission time so it self-adjusts as the pool changes.  A job that runs out of space mid encode wastes the whole encode, so if the pool is small, lower MAX_JOBS rather than the multiplier.  There is no equivalent guard for memory.  That was considered and declined:  the prediction needs a constant nobody has measured and frame area dominates it, so sizing CONTAINER_MEM is the operator's call and an OOM kill mid encode is an accepted failure mode.

## 5.  Configuration

The environment is the entire configuration surface.  No config file, no host assumptions.  Every variable below is read once at startup, validated, and echoed into the log and onto '/api/status', so what the container thinks it was configured with is always visible without exec-ing into it.

```
MEDIA_ROOT           /media            required rw, the one mount everything derives from
MEDIA_ENCODE         <root>/encode     optional, per-title work area on faster storage
MEDIA_CONFIG         <root>/config     optional, state.db, lock, cache, logs
LIBRARY_MOVIES       unset             ro movie library
LIBRARY_TV           unset             ro tv library
CERT_DIR             /certs            ro, holds the TLS certificate and key
TLS_CERT_FILE        fullchain.pem     certificate name within CERT_DIR
TLS_KEY_FILE         privkey.pem       private key name within CERT_DIR
PUID / PGID          required          identity the supervisor drops to
RENDER_GID           unset             supplementary group for /dev/dri, GPU off if unset
RENDER_NODE          /dev/dri/renderD128  render node the GPU probe and QSV encoder use
OUTPUT_CODEC         hevc              hevc or av1
MAX_JOBS             3                 concurrent titles, also bounded by encode free space
GPU_SLOTS            1                 concurrent GPU encodes
CPU_SLOTS            1                 concurrent CPU encodes
ENCODE_HEADROOM      3.0               multiple of source size required to admit a job
ENCODE_THREADS       0                 0 autodetects from the cgroup CPU quota
CRF                  18                default quality target
TV_ENCODE_SD         0                 1 re-enables SD television encoding
WEB_PORT             443               HTTPS only, there is no HTTP listener
DRY_RUN              0                 1 logs every intended action and performs none
POLL_INTERVAL        60                import watch interval, seconds
MTIME_QUIET          120               seconds untouched before a file counts as stable
LOCK_WAIT_TIMEOUT    0                 seconds to wait for the instance lock, 0 waits forever
LOCK_WAIT_INTERVAL   15
GRAIN_THRESHOLD      0.18              denoise delta above which a source counts as grainy
LOG_LEVEL            info              info or debug, see section 20
```

Adding a setting means adding it to 'Config.as_dict()' as well, or it silently vanishes from the startup banner and the status endpoint, which is where anyone debugging looks first.  NOTHING ENFORCES THAT TABLE.  It was machine-checked once and is not any more, so a setting added to the code and not to this table drifts silently until someone reads both.  TESTPLAN.md case T-01 compares the two by hand at startup.

### Read outside Config

These are read at import time in the module named, are NOT validated, and do NOT appear in the banner or on '/api/status'.  They exist as seams for substituting a binary, not as configuration.

```
FFPROBE      ffprobe       probe.py
FFMPEG       ffmpeg        media.py
MKVMERGE     mkvmerge      probe.py, media.py
MKVPROPEDIT  mkvpropedit   media.py, tags.py
MKVEXTRACT   mkvextract    tags.py
VAINFO       vainfo        gpu.py
```

TWO SETTINGS EXIST IN BOTH PLACES.  'RENDER_NODE' has a module-level fallback at 'gpu.py' and 'encode.py', and 'GRAIN_THRESHOLD' has one at 'media.py'.  In both cases the Config value wins whenever a cfg is passed, and the module constant only serves a direct call that passes none.  This is deliberate and it is not drift, but a change to either has to be made in both places.

## 6.  The chain

```
DETECTED     size stable across two polls, mtime quiet
PROBED       one ffprobe pass, classify movie or tv
SCREENED     minimum standards           fail -> HELD, overridable
IDENTIFIED   provider ID resolution      fail -> HELD; transient -> retry with backoff
COMPARED     against library incumbent   loss -> QUARANTINE, ambiguous -> HELD
STAGED       copy into the encode job directory
REMUXED      container conversion if needed, then language strip, then flag repair
TAGGED       movie MOVIE block, or the three-level TV hierarchy
READY        the readiness gate           fail -> HELD
ENCODING     the encoder is running, progress and ETA on /api/status
ENCODED      route per section 14, or pass through
VERIFIED     duration, statistics
PUBLISHED    move to complete/, TERMINAL as far as a user is concerned
CLEANUP      source to quarantine, encode job directory wiped
```

PUBLISHED IS THE END OF THE PIPELINE FOR A PERSON;  CLEANUP IS HOUSEKEEPING.  Everything after PUBLISHED operates on the pipeline's own working areas and touches nothing the operator collects.  A CLEANUP failure therefore must never present a published title as failed.  Section 21 records the incident:  the encode, the verification and the publish had all completed and only the final move failed, and "the title landed in FAILED with the work already done."  The move fallback fixed that cause;  treating a housekeeping step as the terminus was the shape that let it mispresent, and PUBLISHED being terminal is what fixes the shape.  'state.COMPLETE' is the pair, and the dashboard reads it as one figure.

THE TAG BLOCK IS WRITTEN TWICE, BEFORE AND AFTER THE ENCODE.  ffmpeg's Matroska demuxer renders a targeted tag into the global metadata dict as 'TARGETTYPE/NAME' and the muxer writes it back untargeted, so '-map_metadata 0' on the encode flattens a correct block into exactly the defect section 12 describes.  The pre-encode write and its readiness gate stay, because they are what stops a title before it costs encoder time.  The post-encode write is what makes the published file correct, and readiness runs a second time inside VERIFIED against the file that actually ships.  The carry-forward set is read from the pre-encode file:  'tags.carry_forward' skips any name containing a slash, so reading the flattened output would silently drop the scraped ACTOR, DIRECTOR, GENRE and SYNOPSIS keys.

TWO ORDERINGS ARE LOAD BEARING.

IDENTIFICATION HAPPENS EARLY, before any encode, so the canonical name is settled and a title that cannot be identified costs no encoder time.  The comparison runs before the encode for the same reason:  a clear loser is quarantined without an encode being spent on it.

THE LANGUAGE STRIP RUNS BEFORE THE ENCODE.  The encoder maps every audio track and would otherwise carry foreign audio into the final file.

'/media/import' is the only watched entry point.  Nothing else is mounted, so nothing enters the chain that was not deliberately placed there.

A file must be size-stable across two polls AND untouched for MTIME_QUIET seconds before it is detected.  Files ending in '.part' and files beginning with a dot are ignored outright.

## 7.  Minimum standards

The first gate.  It decides whether a file is worth any identification or encoding effort at all.

```
BOTH KINDS
  ffprobe readable, at least one video stream
  at least one audio track tagged eng or und
  not a 25 fps PAL speed-up of film material
  baked-in letterbox no worse than 20 px
  not a sample or extras file

MOVIES
  display resolution at least 1920x800, computed width * SAR / height
  runtime at least 40 min

TELEVISION
  SD accepted, since no HD master exists for much of the library
  runtime at least 15 min
```

THE HEIGHT FLOOR IS 800, NOT 1080, AND THE WIDTH FLOOR IS WHAT REJECTS SD.  A 2.40:1 scope master is 1920x800 and a 2.35:1 is 1920x818;  neither has bars and neither is 1080 tall.  Measured 2026-09-08:  a correctly cropped Blade Runner 2049 and a Return of the Jedi at 1920x816 were both held as "below the 1920x1080 floor", so the floor as written rewarded the file that wasted a quarter of every frame on black.  The width floor of 1920 is what continues to reject 720p, NTSC and PAL DVD, all of which display far narrower.

The 40 minute movie floor exists because a 10 minute bonus featurette once qualified as a disc's main feature and produced two wrong rips.  The floor is the guard, and section 26 records the ARM setting that was the actual cause.

Failures go to 'hold/' with a written reason, never silently to quarantine.  The UI carries a per-title override that forces a title through anyway.

The letterbox check is cheap-first:  only a frame whose display aspect is 16:9 or 4:3 can hide baked in bars, so a warning is raised on those and the expensive cropdetect runs later, on candidates only.  Bars under about 20 px are not worth acting on.

## 8.  New versus incumbent comparison

Runs after identification and before any encode.  Ordered gates, first clear difference decides.

```
1  HDR or Dolby Vision present    losing it is never an upgrade
2  display pixel count            w * SAR / h, never stored dimensions
3  baked-in letterbox             larger real picture area wins, gate 2 defers to it
4  audio maximum channel count    5.1 beats 2.0
5  bit depth                      10-bit beats 8-bit
6  source pedigree                remux > encode > web
```

Clear win proceeds.  Clear loss goes to quarantine with no encode spent.  Level or contradictory goes to held with a side-by-side attribute table in the UI.

Gate 6 is last on purpose.  Source pedigree is inferred from release naming, which is exactly the kind of signal this project distrusts everywhere else, so it only ever breaks a tie that the five measurable gates could not, and it is flagged as a weak signal when it does.

Gates 2 and 3 need both sides to be measurable.  When one side is missing the gate is skipped and the skip is recorded in the notes rather than silently treated as a tie.

THE COMPARISON CARRIES EVERY ATTRIBUTE THE PIPELINE MEASURES, NOT ONLY THE SIX IT GATES ON.  Anything measured and dropped is a defect rather than an omission.  'compare.MEASURED' is the list and it is what the UI renders;  a row that differs with no gate against it is marked as such, because that is the case where the pipeline saw something and had no rule for it.  This was written after a title won on audio channel count while the incumbent's real disqualifier, a 25 fps PAL speed-up, was invisible to all six gates.

'picture_pixels' had been hardcoded to None since the gate was written, so gate 3 had never fired on any title in the container's history and every comparison emitted the skip note.  Cropdetect now runs at COMPARED on both sides, and only when either side is a letterbox candidate, keeping the cheap-first rule from section 7.

GATE 2 DEFERS TO GATE 3 WHENEVER EITHER SIDE CARRIES BAKED-IN BARS.  Display pixel count counts black bars as picture and gate 3 exists to discount them, so a gate 2 that decides first has answered the letterbox question on the wrong number.  Measured 2026-09-08:  an incoming Blade Runner 2049 at a correctly cropped 1920x800 was quarantined against an incumbent stored 1920x1080 carrying 280 px of bars.  Both hold an identical 1,536,000 px of real picture, and gate 2 decided on a margin that was entirely black.  Cropdetect had already measured those 280 px at COMPARED and the comparison discarded the number.  Gate 2 now records a defer note and gate 3 decides.  On that title the verdict stays a loss and moves to gate 5, bit depth 8 against 10, which is the true disqualifier;  the old path reached the right answer for the wrong reason and would have reached the wrong answer had the incoming file been 10-bit.

THE DEFER CONDITION HAS NO AMBIGUOUS MIDDLE, BY CONSTRUCTION.  'media.CROP_MIN_BARS_PX' and 'standards.LETTERBOX_MAX_BARS_PX' are both 20, so 'letterbox_px' is only ever 0 or 20 and above.

The exposure was never one title.  A 24-file cropdetect sample of the movie library, taken with the bit-depth-scaled limit from section 16, found the 10 to 19 px band empty and three titles at or above 20 px, two of them the same shape as the Blade Runner incumbent:  Suicide Squad at 1920x1080 with 280 px, and Guardians of the Galaxy Vol. 3 at 1920x1016 with 212 px.  A correctly cropped arrival for either was quarantined by the old gate 2.

A PAL SPEED-UP IS DETECTED FROM THE PAIR, NOT FROM ONE FILE.  Equal frame counts within one frame at different frame rates means one side is speed-adjusted and the slower rate is correct.  That works at any resolution.  The single-file check in 'standards' keys on stored height and could not see a 1080p file at 25 fps carrying a 23.976 master's frame count.

The container never touches the incumbent.  It is read, compared against, and left alone.

## 9.  Title authority and the filename transform

THE TAG IS THE PROVIDER'S TITLE, VERBATIM.  Character for character, including ':', '?', '"' and any other punctuation.  Nothing is ever stripped from a tag.

THE FILENAME IS THAT SAME TITLE MINUS ONLY UNSAFE CHARACTERS.  No re-wording, no abbreviation, no reordering, no truncation.  The transform is mechanical and strictly one way:  a filename can always be derived from a tag, a tag can never be derived from a filename.

THE UNSAFE SET is exactly these nine, plus control characters 0x00 to 0x1F:

```
/   \   :   *   ?   "   <   >   |
```

Two further rules apply to the filename component rather than to characters:  it must not end in a period or a space, and it must not be one of the reserved device names CON, PRN, AUX, NUL, COM1 to COM9, LPT1 to LPT9.

Why that set and no more:  XFS and Btrfs forbid only '/' and NUL, while NTFS and SMB forbid all nine.  The libraries are served over SMB, so SMB is the binding constraint.  That is why '?' is removed while '!' is kept:  one is reserved, the other is merely punctuation.

EVERYTHING ELSE IS SAFE AND IS RETAINED:

```
(  )  [  ]  {  }  ,  .  '  -  !  &  #  %  $  ~  +  =  @  ^
and all accented and non-ASCII letters
```

Parentheses and square brackets are structural:  '(Year)' appears in every movie name and '[tvdbid-N]', '[tmdbid-N]', '[imdbid-ttN]' carry provider IDs.  A rule stripping them would contradict every folder in the library.

This keep list is not a preference.  Across 2934 library title portions there are 239 commas, 219 periods, 202 apostrophes, 82 hyphens, 26 exclamation marks and 21 ampersands.  A rule of "strip punctuation from filenames" would condemn about 570 correct files.

REMOVING A COLON.  A colon is REMOVED.  There is exactly one form and it is not a per-title choice.  'Avengers: Endgame' is filed as 'Avengers Endgame'.  This applies identically to movies and television.  Adopted 2026-09-06, superseding an earlier rule that accepted both ' - ' and removal.  The dash form is now non-conforming.

An em dash or en dash becomes ' - '.  A forward slash inside a title becomes '-'.

DIRECTION OF THE TRANSFORM, RESTATED BECAUSE IT IS EASY TO INVERT:

```
tag          Futurama: Bender's Game
folder       Futurama Bender's Game (2008) [tmdbid-13253] [imdbid-tt1054486]
file name    Futurama Bender's Game (2008).mkv
```

Three rules follow:

- Never derive a tag from a folder or file name.  The information needed is already discarded.
- Never assert that a tag equals a filename.  Apply the transform to the tag, then compare results.
- Where a tag and a filename differ by unsafe characters alone, that is expected and is not a defect.  The colon specifically must differ by REMOVAL, not by ' - '.

'app/titles.py' implements this and is removal-only by construction.  It has no permissive mode, because the library migration to the removal form is complete.

## 10.  Naming

### Movies

```
Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv
```

Editions, and ONLY editions, repeat the full folder name plus a label:

```
Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644]/Alien 3 (1992) [tmdbid-8077] [imdbid-tt0103644] - Assembly Cut.mkv
```

Jellyfin does not support Plex's {edition-Name} syntax.  The filename must begin with the exact folder name, then ' - Label'.  Applying the long form to ordinary single-version films once produced 21 wrong filenames.  Do not generalise it.

### Television

```
Show Name (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show Name - SNNENN - Episode Title.mkv

Standard episode        Show - S03E07 - Recluse.mkv
Multi-part episode      Show - S03E02 - Quarantine, Part 1.mkv
Two episodes, one file  Show - S05E01-E02 - Kidnapping.mkv
```

Multi-part episodes use ', Part 1' rather than the provider's marker.  TVDB uses three marker forms and all three must be handled:  '(1)', '(Part 1)' and '(Part One)'.  All three appeared in a single batch, and a converter handling only '(1)' silently leaves the other two intact.  The conversion applies to the Matroska EPISODE title and the segment Info title as well as the filename.

A file covering two episodes takes the range form and drops the part marker entirely.  Naming it as only the first makes Jellyfin report the second as missing.  Its EPISODE PART_NUMBER is the first episode of the range.

## 11.  Provider IDs and episode matching

Movies use tmdbid and imdbid.  Television uses tvdbid and tmdbid, because TVDB governs episode titles and numbering.

Resolution goes through Wikidata, then verification:  'wbsearchentities', then 'Special:EntityData/<QID>.json', reading P4947 for TMDB, P345 for IMDb, P4835 for TVDB and P577 for release date.  Requests are spaced about 3 seconds apart.  CHECK THE HTTP STATUS:  a 429 body fails JSON parsing and looks identical to "not found".

Cross-check before writing an ID.  Fetch the TMDB page and confirm the title matches.  Wikidata provider IDs can be flat wrong:  P4983 for one show held the TMDB movie id of an unrelated 1984 Italian comedy.

Searching a bare franchise name returns the franchise entity rather than the film.  Search 'Title (YYYY film)'.

A YEAR INSIDE A TITLE IS NOT THE RELEASE YEAR.  'Blade Runner 2049 (2017)' carries two year-shaped numbers and the first one is part of the name.  Take the LAST match, not the first, and do not let the pattern consume its trailing delimiter:  in 'Blade.Runner.2049.2017.1080p' the dot after 2049 is also the dot before 2017, so a consuming pattern finds only one match and last equals first.  Both forms resolve correctly with a lookahead.  Titles that are only a year, 1917 and 2012, are unaffected, because the pattern needs a leading delimiter and there is none at position zero.

### The movie identity ladder

A FILENAME IS THE LAST RESORT, NOT THE FIRST.  A file that has been through this pipeline, or that came back out of a library, already states what it is.  'provider.movie_candidates' builds an ordered list of candidates and the first that resolves and verifies wins:

```
1  embedded tag     TMDB, IMDB, TITLE and DATE_RELEASED from the MOVIE-targeted block
2  filename ids     [tmdbid-N] and [imdbid-ttN] parsed out of the file name
3  folder ids       the same, parsed out of the containing folder
4  segment title    the Matroska segment Info title
5  filename         the cleaned file name, the original behaviour
6  parent folder    the containing folder name, skipped when it is the watched root
```

Rung 6 is skipped for a file sitting directly in 'import/', because the parent is then the mount itself and 'import' is not a film.

A CANDIDATE CARRYING BOTH IDS AND A YEAR SKIPS THE WIKIDATA SEARCH, but it does NOT skip verification.  It still fetches the TMDB page and confirms the title appears on it, so a stale or hand-edited tag cannot inject a wrong ID.  A candidate that fails verification falls through to the next rung rather than failing the title.

The rung that produced an identity is recorded in the stage detail, so a wrong match can be traced to its source instead of guessed at.

Measured 2026-09-07:  a fresh ARM rip carries no tags, no segment title and no folder ids, so only rungs 5 and 6 apply to it.  This ladder improves re-imports and library-shaped files;  it does nothing for a disc rip whose name says nothing.

### Finding the incumbent in the library

THE LOOKUP KEYS ON THE PROVIDER ID, NOT ON THE TITLE.  Section 10 puts '[tmdbid-N]', '[imdbid-ttN]' and '[tvdbid-N]' into every library folder name, so an ID match is exact and survives any drift between a stored folder name and what the current transform emits.  The transformed-name prefix match stays as a fallback, and the route that matched is recorded in the stage detail so a name-only match is visible rather than assumed.

Measured 2026-09-08:  Return of the Jedi resolved to 'Star Wars: Episode VI – Return of the Jedi', which the section 9 transform renders as 'Star Wars Episode VI - Return of the Jedi' because an en dash becomes ' - '.  The library folder is 'Star Wars Episode VI Return of the Jedi (1983) [tmdbid-1892] [imdbid-tt0086190]', with no dash at all, so the prefix match failed and the title was compared against nothing before taking a full encode slot.  The transform was correct and the library entry predates it.  Both sides carried tmdbid 1892 and it was never consulted.

A MISS AND A GENUINELY NEW TITLE MUST NOT LOG THE SAME SENTENCE.  'no incumbent, treated as new' read identically whether the library held nothing or the lookup had failed, which is the shape section 21 warns about when selection rests on a single fallible query.  The COMPARED detail now separates four outcomes:  no library mounted, a folder count scanned with nothing matched, a match by provider ID, and a match by folder name.

### Choosing the release year

P577 IS NOT A SINGLE VALUE.  A film routinely carries several release claims, one per country or event, and Wikidata returns them in no meaningful order.  Taking the first is wrong.

Q1259032, Futurama: Into the Wild Green Yonder, as returned:

```
+2008-01-01  precision 9   rank normal   no country
+2009-02-24  precision 11  rank normal   United States
+2009-03-20  precision 11  rank normal   Germany
```

The first is a year-only placeholder and the film is a 2009 release.  Taking claim zero produced 2008 and wrote it into the folder and the file name, so the error reached the library rather than merely the log.  Blade Runner carries the same shape:  four precision-11 claims and a '+1982-00-00' placeholder.

'provider._best_date' selects instead:  drop deprecated rank, prefer preferred rank when present, then the highest precision, then the earliest date.  Verified against four entities.

Lookups are cached on disk under 'config/cache', and the cache is consulted before any request is made.  INTERNET ACCESS IS REQUIRED.  Section 2 forbids guessing a provider ID, so identification is mandatory and there is no offline mode:  a provider that cannot be reached raises ProviderError, which the orchestrator treats as transient.  The title then retries five times over roughly 62 minutes with exponential backoff and holds with the reason written.  A title that cannot get metadata ends in 'hold/' and waits for a person, which is the intended outcome and not a failure of the pipeline.

### Episode order

AIRED ORDER, AUTOMATICALLY, FOR EVERY SHOW.  No per-show decision and no held gate.  This is a deliberate choice and it is an accepted risk, not a covered one:  a show whose disk content is genuinely in DVD order will be numbered as aired and nothing will stop it.  Where per-season counts disagree with aired order the show is still processed and the mismatch is surfaced as a warning.

### Matching

MATCH EPISODES BY TITLE, NEVER BY THE NUMBERING IN SOURCE FILENAMES.  Release groups renumber when they collapse a two-part episode into one file, and everything after silently shifts.  One Voyager season numbered Caretaker as a single s01e01, putting every following file one behind TVDB.  Match normalised episode titles against the provider list, then derive SNNENN from the match.

Fuzzy fallback at a difflib cutoff of 0.82.  Release filenames carry typos:  one show alone had six ('No Sequitur', 'Persisitence of Vision', 'Dreadnaught', 'Darklin', 'Worse Case Scenario', 'Vis a Vis').  Fall back to source numbering only when exact and fuzzy both fail, and log every fallback.

A title carrying no part marker that matches a marked pair resolves to the FIRST episode of the pair, not an arbitrary one.  Measured 2026-09-07:  'Caretaker' tied against 'Caretaker (1)' and 'Caretaker (2)' and fuzzy matching picked part 2.  Range extension then relies on this.

Episode ranges appear in source filenames as 'e17-18', 'e01-2', 'e16-18', 'e44+45', 'E01E02' and 'e15&16'.  A regex handling one form silently reports episodes missing that are present.

Only collapse into a range when the source really is one file covering two episodes.  Assign every file to a distinct episode first, then extend a file to a range only if the following episode is still unclaimed and shares the same base title.

## 12.  Matroska metadata

Three separate title fields exist and are easily confused.

SEGMENT INFO TITLE, set with 'mkvpropedit FILE --edit info --set title='.  The plain title, no year, real punctuation preserved.

GLOBAL TAGS TITLE, set with 'mkvpropedit FILE --tags global:file.xml'.  Must agree with the segment title.  ffprobe reports this one in preference when present.

TRACK NAME, set with 'mkvpropedit FILE --edit track:v1 --set name='.  For tracks the property is 'name', NOT 'title'.  '--delete title' on a track is a parse error and mkvpropedit aborts the entire command, so every other edit in that invocation silently fails too.

### Movie tag shape

A single MOVIE-targeted Tag carrying exactly TITLE, TMDB, IMDB and DATE_RELEASED.  TITLE equals the segment Info title.  mkvpropedit omits '<TargetTypeValue>50</TargetTypeValue>', so VERIFY BY TargetType NAME, NEVER BY GREPPING FOR THE NUMBER.

Some files carry a large untargeted block of scraped metadata instead:  ACTOR, DIRECTOR, GENRE, PRODUCER, SYNOPSIS, LAW_RATING and so on.  THESE ARE LEGITIMATE AND MUST BE CARRIED FORWARD.  '--tags global:' replaces untargeted tags and would destroy every one of those fields.  When rewriting a movie tag, carry forward any untargeted key that is not part of the canonical set.

Only ENCODER, COMMENT, MAJOR_BRAND, MINOR_VERSION, COMPATIBLE_BRANDS, HANDLER_NAME, VENDOR_ID, CREATION_TIME and SOFTWARE are safe to drop.  Those are remux leftovers.

### Television tag hierarchy

Three levels in addition to the segment title.  Getting them inverted is an easy and previously made mistake.

```
Target 70, COLLECTION   TITLE = show name, plus TVDB and TMDB ids
Target 60, SEASON       TITLE = 'Season N', PART_NUMBER = N
Target 50, EPISODE      TITLE = episode title, PART_NUMBER = episode number
```

The segment Info title is the EPISODE title, matching the title portion of the filename.  For a range file the EPISODE PART_NUMBER is the first episode of the range.

### Flattened tag blocks

A subtler failure of the same hierarchy.  Every value lands inside ONE untargeted Tag whose Simple names carry the level as a slash path:  'COLLECTION/TITLE', 'SEASON/PART_NUMBER', 'EPISODE/TITLE'.  Jellyfin does not read that as a hierarchy.

It defeats every cheap check.  A grep for COLLECTION passes, all strings are present and correct, and an XML parse succeeds.  THE ONLY RELIABLE TEST IS STRUCTURAL:

- assert all expected TargetType names appear on their own Tag elements
- assert no Simple Name anywhere contains a slash

An audit of 2276 episodes found 329 affected across four shows, and 51 of 213 movies carried the same defect as 'MOVIE/TITLE'.  The data in a flattened block is normally correct and only the shape is wrong, so the repair is structural, not a re-derivation.

### Statistics

Every file carries per-track BPS, DURATION, NUMBER_OF_FRAMES and NUMBER_OF_BYTES, written with '--add-track-statistics-tags'.

USE '--tags global:' AND NEVER '--tags all:'.  The all: form replaces every tag in the file and destroys per-track statistics.

The global: form replaces only untargeted tags, so per-track statistics normally survive.  THIS PROTECTION IS NOT UNIVERSAL.  Statistics are only safe when they are track targeted; some files store them as global tags and those ARE destroyed.  One film lost every statistics tag to exactly this while its sibling survived the identical command in the same batch.  ALWAYS re-check the byte-sum ratio after writing global tags and re-run '--add-track-statistics-tags' where it comes back zero.  'tags.refresh_statistics' does this and returns the ratio.

A trailing '--add-track-statistics-tags' is REQUIRED after any container conversion, after mkvpropedit-only edits, and after an encode.  Do not reason about it, check the ratio and act on the number.

## 13.  Tracks:  flags and language policy

### Flags

Audio tracks:  EXACTLY ONE default, and it must be the primary track rather than a commentary.  Assert the count is exactly one rather than merely testing that some default exists.  Files with several audio tracks and no default let the player choose arbitrarily, which on one release meant landing on the director's commentary.  More than one default is the common scene-release failure:  one release flagged both its TrueHD Atmos and AC3 track as default.

Subtitle tracks:  no default, in any language.

Forced subtitle tracks are exempt and keep their default.  Key that exception off the actual forced_track property and NEVER off the track name, since names like 'SDH' and 'Force' are inconsistent scene conventions.

### Language

English only.  Non-English audio and subtitle tracks are dropped at ingest.

```
KEEP_LANGS = eng en und
```

VIDEO TRACK LANGUAGE IS 'eng', not 'und'.  The one exception is cover art:  a V_MJPEG track is a poster, not video, and stays 'und'.

'und' is kept and always will be, because an untagged track must never be assumed foreign.

THE PURPOSE IS CONVENIENCE, NOT DISK SPACE.  A clean single-language track list means Jellyfin presents one audio option rather than a menu of thirty.  Space reclaimed is incidental and is NOT a measure of success.  Do not report bytes saved.  Report track outcomes:  foreign tracks gone, exactly one default audio, no subtitle default, nothing truncated.

TRAP:  an untagged foreign track survives, because untagged maps to 'und' and 'und' is kept.  A subtitle named 'Chinese (Cantonese)' with no language tag would be kept and the run would report success.

TRAP:  the policy covers audio and subtitles equally.  Selecting on foreign audio alone leaves every file whose audio is clean English but whose subtitles are not.  That mistake once left 269 files untouched after a run that reported 199 of 199 successful.  'media.strip_foreign' keys on track type and language, so both are treated identically by construction.

A LANGUAGE STRIP CAN MAKE A FILE LARGER.  ffmpeg '-c copy' writes zlib-compressed PGS subtitles back uncompressed, so surviving tracks expand.  One film grew 136 MB despite dropping six tracks.  A file that grew is NOT a failed remux and is not a finding.

## 14.  Encoding and the encoder router

### The router

Evaluated in order, first match wins.  Implemented in 'encode.select', which is a pure function of the probed attributes so it is fully testable without media.

```
1  source codec is hevc or av1          PASSTHROUGH
2  television and the source is SD      PASSTHROUGH
3  Dolby Vision RPU present             libx265    on any setting
4  OUTPUT_CODEC=av1 and grainy          libsvtav1  CPU
5  OUTPUT_CODEC=av1                     av1_qsv    GPU
6  grainy                               libx265 aq-mode=4:tune=grain
7  otherwise                            libx265 aq-mode=3
```

GATE ORDER IS LOAD BEARING AND BREAKS SILENTLY IF DISTURBED.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails.  TESTPLAN.md cases T-31 to T-38 exercise one title per gate and read the resolved gate back from the API;  run them after touching this function.

Gate 1:  an already-AV1 file is never transcoded back to HEVC.

Gate 2:  SD television is never re-encoded.  An SD source has little to gain and a generation of quality to lose.  SD means display height below 720, computed from width times SAR over height, so an anamorphic PAL DVD rip is classified on what it actually displays.  TV_ENCODE_SD re-enables it.

Gate 3:  an AV1 re-encode discards the Dolby Vision RPU, because AV1 Dolby Vision is profile 10 and effectively nothing plays it.  DV titles always take the x265 path, on every setting.  This is why the x265 path can never be retired.

PASSTHROUGH MEANS NO VIDEO RE-ENCODE, NOT NO PROCESSING.  This is the easy misreading and it would strand files in their source container.  A passthrough title is still remuxed to Matroska, language stripped, flag corrected, tagged and given statistics.  An SD AVI rip arriving in 'complete/' still as an .avi is a bug.

### x265 parameters

Settled on a nine-film batch, 2026-08-27 and 2026-08-28.  DO NOT RETUNE THESE.

```
libx265, preset slow, crf 18, pix_fmt yuv420p10le
-x265-params <aq>:psy-rd=2.0:psy-rdoq=1.0:deblock=-1,-1
aq = aq-mode=4:tune=grain on film sources, aq-mode=3 otherwise
```

THE THREADING FIGURE IS NOT PART OF THAT TUNING.  'pools=' is appended to the same '-x265-params' string, and 'lp=' to '-svtav1-params', from ENCODE_THREADS divided by CPU_SLOTS.  The quality settings above are settled by measurement and must not be touched;  the thread count is derived from the environment and is expected to differ between deployments.  A built command that carries 'pools=' has not been retuned.

USE tune=grain ON FILM SOURCES.  This is the single setting that separated the batch and the spread is not subtle.  The two grain-tuned jobs produced the lowest and third-lowest bitrates at identical CRF.  The one grain-heavy 35mm source that ran plain 'aq-mode=3' produced the highest at 9,697k, nearly double another title on the same CRF and aq-mode.  Without the grain tune, x265 reads film grain as detail worth preserving and pays for it frame by frame.

JUDGING FROM RELEASE YEAR IS NOT RELIABLE.  One of the grain-heavy sources is a 2015 title shot on 35mm.  Check the source, not the date.

TEN GIGABYTES IS A GUIDE, NOT A LIMIT.  Nothing fails for going over.  Treat it as a prompt to check whether the source is film and the grain tune was missed.  Raising CRF to pull a file under the number is the WRONG move:  the size is a preference, the quality target is not.  Size figures are a planning baseline and are the wrong measure of whether a run succeeded.

### AV1 parameters

STARTING POINTS, NOT SETTLED VALUES.  The calibration batch has not been run.

```
av1_qsv     -preset veryslow -global_quality 26, p010le via hwupload
libsvtav1   -preset 4 -crf 24 -pix_fmt yuv420p10le -svtav1-params tune=0:film-grain=8
```

'tune=0' is the subjective mode; the default 'tune=1' targets PSNR and is wrong here.  'film-grain' is the AV1 answer to 'tune=grain':  grain is synthesised at decode rather than paid for frame by frame.  Its 'film-grain-denoise' companion is a calibration variable, not a settled value.

BEFORE FLIPPING THE AV1 DEFAULT, run a calibration batch and score against the SOURCE, not against the x265 table.  The reference films are already HEVC, so re-encoding them measures a second generation rather than the encoder.  Score with the 'ssim' filter, which is built into ffmpeg and needs no new dependency.  Bitrate alone settles nothing:  AV1 producing a smaller file is the expected outcome and proves nothing.  The question is size at equal SSIM.

### Automatic grain detection

The grain signal cannot come from a hand-written sidecar, because in an automatic chain nobody writes one and every film source would silently lose the largest measured lever in the project.

Measured instead:  a 20 second sample from the middle of the file, encoded twice at a fixed CRF, once clean and once through a light 'hqdn3d' denoise.  Grain is expensive to encode, so a grainy source shows a large size delta and a clean digital source shows almost none.  The ratio is logged for every title so a bad threshold is visible rather than silent.  GRAIN_THRESHOLD tunes it.

An 'encode.job' sidecar beside the source overrides the probe and wins:

```
film=1                  force the grain path, film=0 forces the clean path
codec=av1               per-title output codec
crf=17                  per-title quality target
crop=1920:804:0:138     skip cropdetect and use this
```

### Two traps in the command shapes

MAPPING.  Explicit maps, never '-map 0'.  A bare '-map 0' hands a V_MJPEG cover-art track to the video encoder, which re-encodes a poster as video.  Cover-art mjpeg is legitimate and must be carried, not encoded.  Use '-map 0:v:0 -map 0:a -map 0:s? -map 0:t? -map_chapters 0'.

THE SDR STAMP IS A VALUE, NOT A FLAG.  An untagged SDR source is stamped smpte170m only when it is SD;  an untagged HD source is stamped bt709.  The original rule was written for untagged NTSC DVD rips and the code applied it at any resolution, which put a 601 matrix on a 1080p master and shifted every saturated colour.  SD is 'encode.is_sd', the same predicate router gate 2 uses, and it is defined once.

COLOUR FLAGS, AND THE FIX THAT MUST NOT BE GENERALISED.  On the x265 path, ffmpeg's '-color_primaries', '-color_trc' and '-colorspace' suppress what '-x265-params' sets, so only the matrix lands.  Those three flags must NOT be passed on the x265 path; colour goes inside '-x265-params' instead, with '-color_range tv' alongside.

THAT FIX IS SPECIFIC TO THE X265 PATH.  Neither av1_qsv nor libsvtav1 accepts colour properties through a params string, so on both AV1 paths those same three ffmpeg flags are the correct and only mechanism for the smpte170m stamping of untagged NTSC DVD rips.  Generalising the removal would silently drop the stamping.  TESTPLAN.md case T-58 checks the colour properties on the output file for a tagged source, an untagged NTSC rip and an HDR source, which is where this mistake would surface.

DO NOT APPLY SDR COLOUR ASSUMPTIONS TO HDR MATERIAL.  The smpte170m stamping is correct for untagged NTSC DVD rips and is WRONG for anything bt2020.

## 15.  Container remuxing

NEVER use 'ffmpeg -map 0' when remuxing MP4 to MKV.  Many HandBrake MP4 rips carry a 'bin_data' QuickTime chapter stream that Matroska rejects outright, failing the remux; 135 of one show's 242 files had one.  Use '-map 0:v -map 0:a -map 0:s? -map_chapters 0'.  Dropping bin_data is lossless because the chapters live in the MP4 chapter atom and ffmpeg carries them into Matroska natively.  ALWAYS ASSERT THE CHAPTER COUNT IS PRESERVED;  'media._mp4_to_mkv' fails the remux when it changes, and TESTPLAN.md case T-41 records both counts.

Old Xvid and DivX AVI rips often need '-fflags +genpts -bsf:v mpeg4_unpack_bframes'.  Allow about 2 seconds of duration tolerance, not 1:  genpts recomputes timestamps and routinely lands a second either side with every packet preserved.  Genuinely damaged sources drifted 14 to 21 seconds.

Compare packet counts to distinguish a damaged source from a bad remux.  Two files drifted 14 to 21 seconds while nb_read_packets was identical in and out; the AVI header over-claimed frames by exactly the drift at 25 fps.  The remux was faithful.

ffmpeg cannot infer a container from a temp extension like 'file.mkv.part' and fails at muxer init.  ALWAYS PASS '-f matroska' EXPLICITLY when writing to a temp name.  Without it the remux silently falls through to a fallback, so the intended code path never runs while per-file verification still passes.

mkvmerge, unlike ffmpeg, detects Matroska by content, so it works fine on a '.part' temp name.

mkvmerge silently makes a muxed-in sidecar subtitle the default track.  After any sidecar mux, clear the flag explicitly.

A REMUX INHERITS THE SOURCE CONTAINER'S TITLE.  ffmpeg copies the input's title metadata into the output, so a mux from a scene release arrives carrying a segment title like 'Grease.2.1982.PROPER.1080p.BluRay.H264.AAC-RARBG'.  Set the segment Info title explicitly after any remux rather than assuming the new file has none.

ffmpeg does NOT compute track statistics.  It carries forward whatever the source held.  A container conversion from a source holding no Matroska statistics writes NONE at all:  measured byte-sum ratio 0.0000 on both AVI to MKV and MP4 to MKV.

## 16.  Verification and quality signals

```
Encode completed        absence of .part files
Nothing truncated       compare VIDEO STREAM duration
Stream fidelity         compare packet counts in versus out
Corruption              full decode with 'ffmpeg -f null'
Statistics accuracy     sum NUMBER_OF_BYTES, compare against file size
Letterboxing            metadata first, cropdetect only on real candidates
```

TRAP:  COMPARE VIDEO STREAM DURATION, NOT CONTAINER DURATION.  After a language strip the container figure can drop by several minutes with nothing lost, because the longest stream was a subtitle track that got removed.

WHAT 'orchestrator._verify' ACTUALLY RUNS:  video stream duration, video packet count in against out, the statistics byte-sum ratio, and the structural tag readiness check.  The table above is the standard;  the full decode scan is deliberately NOT implemented, because it costs a complete read of the output per title and the packet count was judged to carry enough of the signal.  Section 4's I/O budget names the packet count pass rather than a decode pass so the two agree.  Measured 2026-09-08 on the first published title, an 89 minute encode preserved 128,424 video packets exactly, which is what makes an equality check rather than a tolerance the right shape here.

TRAP:  a decode scan does NOT catch dropped audio.  Surviving packets are valid and there is simply a hole in the timeline.  The reference defect, 193 gaps and roughly 150 seconds of missing audio, passed a clean decode.

### Cropdetect

TRAP, AND IT SILENTLY INVALIDATES WHOLE AUDITS:  cropdetect's 'limit' is read in the source's NATIVE BIT DEPTH, not normalised to 8-bit.  The usual limit=24 is correct for yuv420p, but against a 10-bit source it means 24 of 1023, an 8-bit equivalent of about 6, which sits below video black at 16.  Bars are then never detected and every 10-bit file reports full-frame no matter what it contains.

```
limit = 24 * 2^(depth - 8)      so 96 for 10-bit, 384 for 12-bit
```

Read pix_fmt per file and set the limit from it.  This defect had already corrupted a full 2456 file audit, silently passing 596 high-bit-depth files as clean.  'probe.cropdetect_limit' implements the scaling; use it rather than a constant.

WHEN A DETECTOR AND A DECODED FRAME DISAGREE, BELIEVE THE FRAME.

Anamorphic storage defeats a pure geometry check.  A scope film can be stored 1920x1080 with a non-square sample aspect, giving full 1080 rows of picture and no bars.  Compute display aspect from width times SAR over height; never infer it from stored dimensions alone.

### Quality signals

ONE AUDIO TRACK LABELLED STEREO likely means a bonus feature rather than the main title.  Correct feature rips carry 2 to 5 audio tracks including a 5.1.

A SCOPE FILM STORED AT 1920x1080 means baked-in letterbox.  The real picture may be only 800 or 818 lines, with roughly a quarter of every frame encoded black.

A 4:3 FRAME WHOSE CONTENT FILLS IT is correct, an Academy-ratio or open-matte transfer.  A 4:3 FRAME WITH WIDE CONTENT INSIDE IT is a genuine defect, windowboxed on a modern display.

A LIBRARY ENTRY CAN BE A PAL DVD RIP WEARING AN HD LABEL.  One folder held 720x576 at 25 fps with SAR 64:45 where the DVD had 5.1 audio.  That runs 4 percent fast with audio pitched about a semitone sharp.  Check frame rate and stored dimensions before assuming a folder holds what its name implies.  This is why the PAL speed-up check is in the minimum standards.

## 17.  Known false positives

Do not report these as defects.

CLAIMED BYTES EXCEED FILE SIZE.  Caused by zlib-compressed PGS subtitle tracks:  mkvpropedit reports uncompressed logical bytes while Matroska stores them compressed.  Only a large overshoot, above roughly 1.5x, means genuinely stale tags.

VIDEO BPS EXCEEDS WHOLE-FILE BITRATE.  BPS is computed over the track's own duration, not the container's, so a video track shorter than its container inflates the figure.

A STREAM WITH NO STATISTICS TAGS.  Cover-art mjpeg streams are never tagged.

RUNTIME 8 MINUTES OVER THE PROVIDER'S.  Usually long end credits, or a legitimately different cut.

DUPLICATE NUMBER_OF_BYTES AND NUMBER_OF_BYTES-eng TAGS.  Caused by setting a language tag on a file that already had statistics tags.  Re-run '--add-track-statistics-tags' to normalise.

A DATE_RELEASED THAT IS A FULL DATE RATHER THAN A YEAR.  Scraped-metadata files carry ISO dates such as '1977-05-25' where the canonical shape carries '1977'.  The ISO form is richer, not wrong.

A COLLECTION TITLE EQUAL TO THE EPISODE TITLE.  Legitimate whenever a pilot or TV movie shares the series name.  Test the hierarchy structurally instead.

A TITLE THAT APPEARS TO DIFFER FROM ITS FILENAME.  If the title contains a comma, 'ffprobe -of csv=p=0' wraps the value in literal quotes, so a shell comparison fails on 23 episodes that were correct.  Use an XML parser or a JSON output format.

A TAG TITLE THAT DIFFERS FROM ITS FILENAME BY AN UNSAFE CHARACTER.  The tag keeps the provider's punctuation while the filename omits the nine unsafe characters.  Apply the transform to the tag before comparing.  Comparing mkvextract's raw output against a decoded filename also falsely flags 'Black, White & Brown', because '&amp;' is legitimate XML encoding.

SEASON 00 WITH GAPS IN ITS NUMBERING.  Specials are numbered per TVDB and a library normally holds only some.  Any gap check must exempt season 0.

A FILE THAT GREW AFTER A LANGUAGE STRIP.  See section 13.

## 18.  Dolby Vision and HDR

Files carrying Dolby Vision survive a '-c copy' remux intact, including the DOVI configuration record, dv_profile, dv_level, rpu_present_flag, bl_present_flag and any mastering display metadata.  Verified before and after a language strip on two Profile 8 Level 3 titles.

A DV title is NEVER AV1-encoded.  Router gate 3 sends it to libx265 on every setting, which is what preserves the RPU through an encode.  Profiles 5 and 8 both take that path.

Detection is from the v:0 side data list, reading dv_profile and rpu_present_flag.  Plain HDR10 with no RPU is not Dolby Vision and encodes normally; AV1 carries HDR10 static metadata correctly.

## 19.  Single instance, locking and concurrency

ONE INSTANCE AT A TIME, ENFORCED.  The supervisor takes an exclusive 'flock' on 'MEDIA_CONFIG/mediaimport.lock' before doing anything else.  A second instance pointed at the same mounts WAITS for the first to exit rather than running alongside it.

Why this is not optional:  every other guard in the process is 'threading.Semaphore' or 'threading.RLock', which are process local.  Two supervisors sharing the mounts would sweep each other's encode area and could publish the same title twice.

flock rather than a PID file, because the kernel releases it when the holder dies, so a hard kill needs no stale-lock reasoning.  The PID is written into the file for diagnostics only.

The wait is interruptible.  SIGNAL HANDLERS ARE INSTALLED BEFORE THE LOCK IS ATTEMPTED.  An earlier version registered them after the orchestrator was constructed, so a stop during startup had no handler and would have sat until SIGKILL.

ENCODE JOB DIRECTORIES RECORD THEIR OWNING PID.  The startup sweep reclaims only directories whose owner is no longer alive and leaves a live job alone.  An unconditional sweep would delete an in-flight encode's working directory.

ENCODES RUN UNDER A TRACKED SUBPROCESS AND ARE TERMINATED ON SHUTDOWN.  Without this the supervisor exits while ffmpeg keeps running, reparented to the init process and still writing into the encode area, where the next instance's sweep will find it.  This made the two-instance problem reachable from an ordinary restart, with no second container involved.

PUBLISHING IS ATOMIC.  'paths.publish_file' reserves the destination with 'O_CREAT | O_EXCL', copies into it, then replaces.  The previous check-then-act with 'os.path.exists' followed by 'shutil.move' raced between the three concurrent workers:  the same film dropped in twice under different release names resolves to one provider ID, so one folder and one filename, and the second move overwrote the first.  Quarantine naming uses the same exclusive-create loop.

### Slots

```
GPU encode slots   1     the A310 has one media engine, queueing more at it gains nothing
CPU encode slots   1     x265 preset slow and svt-av1 preset 4 both saturate the allotted cores
```

These are separate from MAX_JOBS, which bounds concurrent titles and is really an encode-space capacity limit rather than a CPU one.

The GPU is shared with whatever else uses it on the host.  At the HEVC default the pipeline does not touch it at all, so there is no contention today.  That changes the moment OUTPUT_CODEC becomes av1, and it is a constraint on making that switch rather than a problem now.

## 20.  Logging

EVERY MODULE LOGS WHAT IT DOES.  Software records its activity;  that is the whole justification and it is not shaped by testing.

Two tiers, switched by 'LOG_LEVEL':

- 'info', the default.  What happened.  One line per meaningful action naming the title, the stage and the outcome.  When a stage fails or degrades the line says why in plain terms.  No command lines, no arithmetic, no per-gate working, no chatter for steps that cannot fail.
- 'debug'.  Why.  Full command lines, per-gate comparisons, candidate scoring, measured figures against their thresholds, derived geometry, raw tool output.

'LOG_LEVEL' is a container variable, so raising verbosity is a deployment setting rather than a code change.

Logs go to stdout and to 'config/logs/mediaimport.log', and the tail is served at '/api/logs'.

Every log line carries the title id where one exists, so a single title's path can be extracted from a run with several jobs in flight.

A POLL THAT FINDS NOTHING NEW IS NOT AN ACTION.  The watcher's 'already claims this path' line fires once per in-flight title per poll and accounted for 223 of 328 lines in a three-title run, while the 108 minute encode that completed inside that same run logged nothing at all.  It is a debug line.  ENCODED and VERIFIED each emit an info line carrying the same outcome text they already write into the stage history, because an encode finishing is the most meaningful event the pipeline has.

CONFIG RESOLUTION IS LOGGED BY 'main', NOT BY 'config'.  'Config' is constructed before logging is set up, so it records where each setting came from and 'main' prints that at debug once handlers exist.  Anything logging from inside 'Config' is discarded.

## 21.  Scripting rules and traps

NEVER GATE A JOB ON A COMMAND-LINE STRING MATCH.  Command lines are a shared, unowned namespace:  a status command, an editor holding the file open, a shell running 'grep', or another container in the same PID namespace can all contain the token.  'pgrep -f "libx26[5]"' once matched an unrelated 'grep libx265' and logged "waiting for CPU" against a job that did not exist.  The bracket trick stops the pattern matching the pgrep's own shell; it does not stop it matching anything else.  Under containers it is worse in both directions:  with a shared PID namespace the scheduler sees every process on the host, and with its own namespace it sees nothing and starts a second encode on top of a running one.  Gate on something the job OWNS.  See section 19.

mkvpropedit REPORTS ITS ERRORS ON STDOUT, NOT STDERR.  A wrapper capturing only stderr gets an empty string and reports a blank failure; one ignoring the exit code as well reports success over work it never did.  Capture both streams and key pass or fail on the EXIT CODE.

MKVPROPEDIT TRACK SELECTORS ARE TYPE-RELATIVE; MKVMERGE TRACK IDS ARE GLOBAL.  In a video plus audio plus subtitle file, mkvmerge -J reports the first audio track as id 1, but its selector is 'a1', not 'a2'.  Deriving the selector by adding one to the global id fails loudly on a single-audio file and fails SILENTLY on a multi-audio file, where 'a2' is a real track and the edit lands on the wrong one.  Count the position among tracks of that type.  'probe.track_selectors' does this.

PARSE FFPROBE OUTPUT FROM STDOUT ALONE.  A helper returning stdout plus stderr broke on files emitting harmless decode warnings, so float() threw and files were falsely failed.  Worse, when the source parsed as None the comparison was skipped entirely rather than failing.

VERIFY TAG CONTENT, NOT JUST PRESENCE, AND PARSE THE XML.  A grep confirming a target existed would have passed all 380 files that had the hierarchy inverted.  Parsing alone is still not sufficient:  a flattened block parses cleanly and carries every expected string, so the assertion must be on STRUCTURE.

IDEMPOTENCY GUARDS MUST KEY ON THE DESTINATION, NOT THE SOURCE.  Asking "is the source file gone?" defers forever on remuxed files, since a remux leaves its source in place.

DELETE OR RETIRE SOURCES ONLY AFTER THE OUTPUT PASSES VERIFICATION.

RE-DERIVE THE FILE LIST AT APPLY TIME rather than reusing one captured during an earlier scan.  Selection, action and verification must EACH derive their own list.

SELECTING THE WORK AND VERIFYING THE WORK MUST USE DIFFERENT CODE PATHS.  Reusing one query for both is how a run reports total success over an incomplete list.

'glob.glob' treats '[i_c]' in a real folder name as a character class and silently matches nothing.  USE os.walk for paths containing brackets, which every '[tvdbid-N]' folder does.

RENAME FAILS ACROSS MOUNT POINTS EVEN ON THE SAME DEVICE.  Linux refuses 'rename(2)' when the two paths are on different mounts, regardless of whether they share a filesystem.  Measured 2026-09-08 while retiring a source after a successful publish:

```
/media/import    dev=42
/media/complete  dev=42
os.replace(...)  OSError [Errno 18] Invalid cross-device link
```

Same device number on both sides and EXDEV anyway, because they were two separate bind mounts of one filesystem.  The obvious reading of "cross-device link" is that the filesystems differ, and that reading is wrong.  The encode, the verification and the publish had all completed;  only the final move failed, and the title landed in FAILED with the work already done.

TWO CONSEQUENCES, BOTH IN THE CODE NOW.  The layout collapsed to one root so the pipeline directories are siblings again, and 'paths.move_file' tries 'rename' first and falls back to copy-then-remove only on EXDEV.  Never assume a move within the container is cheap;  never assume it is expensive either.  Let the kernel answer.

A FAILED MOVE MUST RELEASE ITS OWN RESERVATION.  'unique_path' reserves the destination with 'O_CREAT | O_EXCL' before any data moves, so the failure above left a zero-byte file sitting in quarantine.  'move_file' removes the reservation on every failure path.

DOCKER IGNORE PATTERNS ARE PATH-PREFIX MATCHED FROM THE CONTEXT ROOT, NOT gitignore SEMANTICS.  A '.dockerignore' line of '__pycache__/' excludes only './__pycache__/' and does nothing about 'app/__pycache__/'.  Found 2026-09-07 when a build shipped 18 stale .pyc files into the image.  Use '**/__pycache__/' and '**/*.py[cod]'.

## 22.  Image and build

Base 'debian:trixie-slim'.  Busybox and dash are not sufficient and neither is Alpine:  the tooling relies on GNU coreutils behaviour.

Packages:  ffmpeg, mkvtoolnix, python3, bash, coreutils, findutils, jq, ca-certificates, tini, gosu, libcap2-bin, vainfo, intel-media-va-driver-non-free, libvpl2, libigdgmm12, intel-gpu-tools.  The non-free component must be enabled in the apt sources for the Intel media driver.

'libcap2-bin' provides 'setcap' and is required at build time, not run time.  'openssl' is deliberately ABSENT:  nothing in this image generates a certificate, and adding the package would make that possible.  Leave it out.

BINDING 443 AS A NON-ROOT PROCESS.  The entrypoint drops to PUID, and ports below 1024 need a capability.  The build applies 'cap_net_bind_service' to the resolved python binary, not to '/usr/bin/python3', because that is a symlink and 'setcap' does not follow symlinks.  Docker's default capability set already carries CAP_NET_BIND_SERVICE, so the compose file needs no 'cap_add'.  File capabilities live in extended attributes and are easy to lose silently, so the build verifies with 'getcap' immediately after setting it rather than assuming.

Firmware and the i915 binding come from the host kernel.  The image ships userspace only.

### The build gate

The build FAILS if ffmpeg lacks libx265, libsvtav1 or av1_qsv.

DO NOT DELETE THIS CHECK IF IT FAILS.  It exists so the image cannot ship claiming encoders it does not have.  The escalation order is av1_vaapi, which is the same hardware through VAAPI rather than oneVPL, then jellyfin-ffmpeg from the Jellyfin apt repository.  Pick one at build time and record which in an image label; never let the runtime choose.

VERIFIED 2026-09-07:  Debian trixie's ffmpeg carries libx265, libsvtav1, av1_qsv AND av1_vaapi.  No fallback is needed today.  This had been the single unverified assumption in the design.

### Runtime GPU probe

At startup, 'vainfo' must report VAProfileAV1Profile0 with VAEntrypointEncSlice.  A failed probe does NOT crash.  It marks the GPU degraded, and anything that would have gone to av1_qsv routes to libsvtav1 instead.  At the HEVC default a failed probe changes nothing at all, which is the point:  the GPU cannot break the pipeline.

Every encode logs which encoder actually ran, so a GPU that has quietly stopped being used is visible rather than silent.

## 23.  Versioning and release tags

'x.0.0' is a release.  '0.x.0' is a minor update or a bug fix.  '0.0.x' is a pre-release.  The current version is 0.0.8.

TAGS ARE BARE NUMERIC.  '0.0.8', not 'v0.0.8'.  Nothing in the repository matches on a 'v' prefix, and a tag glob written for one would silently match nothing.

'VERSION' IN 'app/__init__.py' IS THE SINGLE DEFINITION.  A version duplicated into a format string rots silently and then misreports the software to every provider it contacts, which is exactly the defect that produced the placeholder User-Agent this replaced.  One consumer today:  the provider User-Agent, built as 'mediaimport/<VERSION> (+<repo url>)'.  Wikimedia rejects generic and browser-imitating agents with 403, and Wikidata is the first host every identification touches, so an honest three-part string is the reliable choice as well as the truthful one.  A browser User-Agent is not an option here.

A TAG DOES NOT TRIGGER A BUILD.  The workflow is 'workflow_dispatch' only, so pushing a tag publishes nothing.  Images are published by a manual run from the Actions tab.  A tag records a point in history;  releasing is a separate, deliberate act.

## 24.  Validation and testing

THERE IS NO LOCAL TEST SUITE AND THERE SHOULD NOT BE ONE.  A workstation and this container are different environments;  a result from one says nothing about the other, so functionality is validated in the container and nowhere else.

While code is being written, the only validation performed is validation of the code itself:

```
python3 -m compileall -q app          every module parses
python3 -c "import app.main"          the package imports, no circular imports
```

Both are properties of the source.  Neither executes a pipeline stage, touches a file or opens a socket.  The CI workflow runs exactly these two as its 'validate' job, and the image job depends on it, so a syntactically broken tree cannot produce an image.

FUNCTIONALITY IS VALIDATED AGAINST A BUILT CONTAINER, PER 'TESTPLAN.md'.  That plan measures outcome:  every case asserts on a file, a filename, a tag block, a track list, an API response, an exit code or a health state.  No case reads a log line and no case depends on a log level.  Every case is repeatable from a stated starting state.

THE ROUTER CASES ARE THE HIGHEST RISK.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails, so a case confirming an output file exists proves nothing.  TESTPLAN.md states the expected gate and encoder per case and reads both back from '/api/titles/<id>', corroborated against the output file's codec.  'encode.select' is a pure function of a probed-attributes dict, which is what makes the decision readable after the fact.

Colour flags are the clearest case where a fix must not be generalised:  the x265 path must never pass '-color_primaries' while both AV1 paths always must.  TESTPLAN.md checks the resulting colour properties on the output file rather than the command that produced them.

Verify container behaviour by running the container, not by reasoning about it.  The instance lock, the handover between instances and the SIGTERM path are all container cases for exactly that reason.

## 25.  Decisions and their rationale

### x265 is the default output codec, not AV1

AV1 is 25 to 30 percent more efficient and is where this library should end up.  The client fleet is not there.  The current Apple TV 4K is A15-based and has no AV1 hardware decoder, and there is no AV1-capable Apple TV on the market, so this is not a wait-a-few-months problem.

Jellyfin transcodes AV1 to h264 on the fly for a client that cannot decode it.  That is a real mitigation when the transcode is hardware accelerated, but it spends at delivery time some of the quality the AV1 encode paid for, and it contends for the same media engine.  Direct play has no failure mode.

So AV1 is fully built, tested and selectable, and OUTPUT_CODEC defaults to hevc.  Flipping it is a config change plus a calibration batch, not a code change.  That is the entire reason encoder selection is a router rather than a hardcoded ffmpeg line.

Consequence to keep in mind:  four of the seven router paths are dormant at the default, which means they are least exercised at exactly the moment they get switched on.  TESTPLAN.md cases T-35 and T-36 exist to exercise them before that switch is flipped in anger.

### The manual review gate became an automatic readiness check

The old pipeline stopped for a human before encoding.  That gate was load bearing, but it was written as an aid to a person who was going to look anyway.  It is now automatic and holds on failure.  DRY_RUN and the held queue exist to buy back the confidence that gate provided.

### Television is processed automatically in aired order

Chosen deliberately over a per-show held decision.  The protection is the matching method, not the order:  titles are matched against the provider list and the number is derived from the match, so release-group renumbering cannot propagate.  A file that matches neither exactly nor fuzzily still holds, because there is no correct automatic action for a file you cannot identify.

### No third-party Python dependencies

Stated as a decision so it does not erode.  The standard library covers HTTP, SQLite, XML, threading and the web server.  Every dependency added is a supply chain, an upgrade treadmill and an audit burden on an image that otherwise consists of Debian packages and this repository.

### The web UI is unauthenticated, over HTTPS only

Intended for a trusted LAN.  Anyone who can reach the port can force a held title through and quarantine an incoming file.  That is a deliberate tradeoff for a home service, and it is the reason the port should be published deliberately rather than broadly.

TLS DOES NOT CHANGE THAT.  The listener is HTTPS on 443 and there is no HTTP listener and no redirect, but encryption is not authentication:  anyone who can reach the port still has full control.  What TLS buys is that the traffic is not readable in transit, nothing more.

THE CERTIFICATE IS SUPPLIED, NEVER GENERATED.  '/certs' is mounted read only, 'openssl' is deliberately absent from the image, and nothing in the container can create a certificate.  A missing, unreadable or malformed pair is logged and the supervisor exits non-zero before any listener is created, because a silent downgrade to HTTP would defeat the requirement.  With 'restart: unless-stopped' that is a restart loop rather than a running container with a broken UI, and that is the intended behaviour.

THE CONTAINER DOES NOT VALIDATE THE CERTIFICATE IT IS GIVEN.  It does not inspect the issuer and does not verify the chain.  Do not add those checks:  the operator owns what is mounted, and chain validation would reject a private-CA certificate as a side effect while proving very little.

The healthcheck probes '127.0.0.1' with verification disabled.  That is not an accommodation for a weak certificate;  a certificate issued for the service hostname fails hostname verification against a loopback address no matter which CA signed it, and the probe is testing liveness rather than identity.

## 26.  ARM configuration, upstream

Automatic Ripping Machine produces most of what arrives in 'import/'.  It is not part of this container and this repository does not configure it, but its settings determine what the container is handed, so they are recorded here rather than only in the workstation standards.

```
MINLENGTH        2400     titles under 40 minutes are ineligible
SKIP_TRANSCODE   true     ARM keeps MakeMKV output, largest file assumed main feature
RIPMETHOD        mkv      MakeMKV direct
PREVENT_99       false    setting true ejects the disc and rips nothing
MAINFEATURE      inert    HandBrake option;  with SKIP_TRANSCODE true, HandBrake never runs
```

MINLENGTH IS THE SETTING THAT MATTERS.  At the previous value of 600 a 10 minute bonus featurette qualified as a disc's main feature and produced the wrong Cars and Chicken Little rips.  2400 is what makes the 40 minute movie floor in section 7 a second line of defence rather than the only one.

WRONG-TITLE RIPS WERE CAUSED BY MINLENGTH, NOT DRM.  The Cars disc reported 8 titles, not 99, so PREVENT_99 was never in play.  Every 'HB_*' setting does nothing while SKIP_TRANSCODE is true, because HandBrake never runs.

## 27.  Out of scope

Not in this repository, and adding them needs a decision rather than a commit:

- Any write path into a media library.  See section 2.
- YouTube.  Those rips are organised by hand and no naming, tagging or encoding standard applies.
- Music.  No standards are defined for it anywhere yet.
- Host-specific packaging.  No Unraid Community Applications template, no Docker Hub mirror.  The deliverable is the image plus a reference compose file that runs anywhere with Docker and a render node.
- The workstation scripts.  They live in their own tree and continue to run there unchanged.

ONE EXCEPTION TO THE PACKAGING RULE, ADDED DELIBERATELY.  The Dockerfile carries 'net.unraid.docker.icon'.  It is Unraid-specific and inert on every other host.  It is there because a container with no icon makes the Unraid Docker page request a placeholder that does not exist on that build, and the page auto-refreshes:  measured 2026-09-08, that filled the 128 MB '/var/log' tmpfs to 100 percent with 66 MB of syslog and 61 MB of nginx errors.  The container wrote none of it.  The icon lives at 'media/mediaImport.png' and is served from the repository, matching what every other container on that host does.  It is a placeholder and is expected to be replaced.

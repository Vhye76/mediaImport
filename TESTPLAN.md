# TESTPLAN

Execution plan for validating mediaImport against a built container.  Run by hand.

## What this is

This plan measures OUTCOME.  Every case asserts on something the container produced or did:  a file at a path, a filename, a tag block, a track list, an API response, an exit code, a container health state.  No case reads a log line and no case depends on a log level.

Every case is repeatable.  Each states its starting state, its input and its command, so the same case run twice from the same starting state gives the same result.

Nothing here runs on a workstation.  The workstation and the container are different environments and a result from one says nothing about the other.

## Preconditions

Before any case runs:

```
image built                docker build -t mediaimport:local .
five writable mounts        import, encode, complete, hold, config
certificate mounted         /certs holds the certificate and key
RENDER_GID set              stat -c %g /dev/dri/renderD128 on the host
```

'LOG_LEVEL' is not set by this plan and no case depends on it.

DRY_RUN is off unless a case says otherwise.

## Recording

Record the observed value for every case, not just pass or fail.  A run that records "PASS" against 13 groups tells you nothing when the next run differs.  A run that records the filename produced, the byte-sum ratio, the chapter count and the resolved gate can be compared against the run before it.

## Reset between cases

Unless a case says otherwise, reset by stopping the container, emptying import, encode, complete and hold, and leaving config in place.  Cases that require an empty state database say so.

---

## 1.  Startup

**T-01  The banner lists every setting.**

- **Start:**  container stopped.
- **Do:**  'docker compose up -d', then 'docker logs mediaimport'.
- **Expect:**  the config banner lists every name in section 5 of CLAUDE.md.  Record any name present in one and absent from the other.

**T-02  Mounts resolve to the paths given.**

- **Start:**  container running.
- **Do:**  'curl -sk https://localhost/api/status | jq .config'.
- **Expect:**  the five MEDIA_ paths match the container paths in the compose file.

**T-03  The GPU probe reports a result.**

- **Start:**  container running with RENDER_GID set.
- **Do:**  'curl -sk https://localhost/api/status | jq .gpu'.
- **Expect:**  'available' true with a non-empty 'av1_encode_profiles', or 'degraded' true with a 'reason' naming the cause.  Both are valid outcomes;  record which.

**T-04  A missing mount refuses to start.**

- **Start:**  container stopped, one writable mount removed from the compose file.
- **Do:**  'docker compose up', read the exit.
- **Expect:**  the container exits non-zero and the message names the missing variable.  Record which variable was named.

**T-05  A missing certificate refuses to start.**

- **Start:**  container stopped, '/certs' mounted empty.
- **Do:**  'docker compose up'.
- **Expect:**  the container exits non-zero.  No listener is bound on 443.  Confirm with 'curl -k https://localhost/' failing to connect.

**T-06  The instance lock is taken.**

- **Start:**  container running.
- **Do:**  'ls -l <config mount>/mediaimport.lock'.
- **Expect:**  the file exists and contains a JSON object with the running container's pid.

**T-07  The healthcheck reaches healthy.**

- **Start:**  container started within the last two minutes.
- **Do:**  'docker inspect --format "{{.State.Health.Status}}" mediaimport'.
- **Expect:**  'healthy'.  Not 'starting', not 'unhealthy'.

## 2.  Detection

**T-08  Partial files are ignored.**

- **Start:**  empty import.
- **Do:**  place 'Movie.mkv.part', wait two poll intervals.
- **Expect:**  '/api/titles' stays empty.

**T-09  Dotfiles are ignored.**

- **Start:**  empty import.
- **Do:**  place '.hidden.mkv', wait two poll intervals.
- **Expect:**  '/api/titles' stays empty.

**T-10  A stable file is detected on the second poll.**

- **Start:**  empty import, empty state.
- **Do:**  place a valid mkv, wait one poll interval, check '/api/titles', wait a second interval, check again.
- **Expect:**  absent after the first, present after the second.

**T-11  A growing file is not detected.**

- **Start:**  empty import.
- **Do:**  place a file, append to it between two polls.
- **Expect:**  '/api/titles' stays empty until it stops growing.

## 3.  Minimum standards

Each case:  place the described file, wait for it to reach a terminal state, then read '/api/held'.

- **T-12  An SD movie holds.**  A 720x480 movie.  Expect HELD with a reason naming the resolution floor.
- **T-13  A short movie holds.**  A 10 minute movie.  Expect HELD naming the 40 minute floor.
- **T-14  A short episode holds.**  A 5 minute episode.  Expect HELD naming the 15 minute floor.
- **T-15  Foreign-only audio holds.**  A file whose only audio is 'fra'.  Expect HELD naming the audio language.
- **T-16  A PAL speed-up holds.**  720x576 at 25 fps.  Expect HELD naming the PAL speed-up.
- **T-17  Undetermined audio is accepted.**  A file whose only audio is 'und'.  Expect it proceeds past SCREENED.
- **T-18  SD television is accepted.**  A 720x480 episode.  Expect it proceeds past SCREENED.

**T-19  The override forces a held title through.**

- **Start:**  a title held by T-12.
- **Do:**  'curl -sk -X POST https://localhost/api/held/<id>/decision -d {"action":"override"}'.
- **Expect:**  the title leaves HELD and advances past SCREENED.

## 4.  Identification

**T-20  A resolvable title records its IDs.**

- **Start:**  a correctly named film in import.
- **Do:**  wait for IDENTIFIED, read '/api/titles/<id>'.
- **Expect:**  'tmdb' and 'imdb' populated, 'title' set to the provider's title.  Record all three.

**T-21  An unresolvable title holds rather than guessing.**

- **Start:**  a file named so no provider match exists.
- **Do:**  wait for a terminal state.
- **Expect:**  HELD.  No provider ID is recorded.  Record the reason.

**T-22  An unreachable provider retries then holds.**

- **Start:**  container running with outbound network blocked and an empty provider cache.
- **Do:**  place a valid file, wait.
- **Expect:**  the title retries, then reaches HELD after the retry ladder is exhausted.  Record the attempt count from '/api/titles/<id>'.

**T-76  An embedded MOVIE tag identifies without a Wikidata search.**

- **Start:**  empty import.
- **Do:**  copy a published library file into import.
- **Expect:**  resolved, and '/api/titles/<id>' stage detail reads "resolved <title> from embedded tag".

**T-77  A '[tmdbid-N]' folder identifies from the path.**

- **Start:**  empty import.
- **Do:**  place a file inside a folder named 'Title (Year) [tmdbid-N] [imdbid-ttN]', tags stripped.
- **Expect:**  resolved, detail reads "from folder ids".

**T-78  A segment title identifies when nothing else does.**

- **Start:**  empty import.
- **Do:**  place a file whose name is meaningless but whose segment Info title is the real title.
- **Expect:**  resolved, detail reads "from segment title".

**T-79  A nameless file still holds.**

- **Start:**  empty import.
- **Do:**  place a file named 'futurepack-cls.mkv', no tags, no segment title.
- **Expect:**  HELD, reason "provider ID could not be resolved and must never be guessed".

**T-80  A wrong embedded ID is rejected and the ladder continues.**

- **Start:**  empty import.
- **Do:**  place a file whose MOVIE tag carries a TMDB id belonging to a different film, with a correct filename.
- **Expect:**  the embedded rung is rejected, the filename rung resolves, detail reads "from filename".

**T-81  A file in the watched root does not use the parent folder.**

- **Start:**  empty import.
- **Do:**  place an unidentifiable file directly in 'import/'.
- **Expect:**  HELD.  No lookup is ever attempted for the term "import".

**T-82  The release year is the release, not the placeholder.**

- **Start:**  empty import, empty provider cache.
- **Do:**  identify Futurama: Into the Wild Green Yonder.
- **Expect:**  year 2009, not 2008.  Published folder reads '(2009)'.

**T-83  DRY_RUN completes a title.**

- **Start:**  'DRY_RUN=1', a resolvable file in import.
- **Do:**  wait for a terminal stage.
- **Expect:**  RETIRED, every stage logging intent, no file created anywhere, and no FAILED state.

**T-84  Discard on a missing source removes the row.**

- **Start:**  a held title whose source file has been renamed away.
- **Do:**  POST 'discard' against it.
- **Expect:**  the row is gone from '/api/titles', nothing written to quarantine, and the response action reads "forgotten".

## 5.  Comparison against the incumbent

Each case needs a prepared library file and a prepared incoming file that differ on exactly one gate.

- **T-23  Gate 1, losing HDR is a loss.**  Incumbent HDR, incoming SDR.  Expect QUARANTINED, incoming file present under 'complete/.quarantine'.
- **T-24  Gate 2, higher resolution wins.**  Incoming 2160p against 1080p incumbent.  Expect it proceeds.
- **T-25  Gate 3, larger picture area wins.**  Incoming without bars against a letterboxed incumbent.  Expect it proceeds.
- **T-26  Gate 4, more audio channels wins.**  Incoming 5.1 against 2.0.  Expect it proceeds.
- **T-27  Gate 5, greater bit depth wins.**  Incoming 10-bit against 8-bit.  Expect it proceeds.
- **T-28  Gate 6, better pedigree breaks a tie.**  Identical but for release naming.  Expect it proceeds and the comparison notes record the weak signal.
- **T-29  A level pair holds.**  Two files identical on every gate.  Expect HELD, and '/api/titles/<id>' carries a comparison object.

**T-30  No library mounted skips the comparison.**

- **Start:**  compose without the library mounts.
- **Do:**  place a title that exists in the library.
- **Expect:**  it proceeds, and '/api/titles/<id>' records that the comparison was skipped.

## 6.  Routing

Each case:  place one title matching the gate, wait for ENCODED, then read the gate and encoder back from '/api/titles/<id>' and confirm the output file's codec.

```
T-31  gate 1   an hevc source           expect passthrough, output still hevc, not re-encoded
T-32  gate 1   an av1 source            expect passthrough, output still av1
T-33  gate 2   an SD episode            expect passthrough, output codec unchanged
T-34  gate 3   a Dolby Vision title     expect libx265, output carries the DOVI record
T-35  gate 4   OUTPUT_CODEC=av1, grainy expect libsvtav1, output av1
T-36  gate 5   OUTPUT_CODEC=av1, clean  expect av1_qsv, output av1
T-37  gate 6   a grainy source          expect libx265, output hevc
T-38  gate 7   a clean source           expect libx265, output hevc
```

**T-39  A passthrough title is still processed.**

- **Start:**  an SD AVI episode.
- **Do:**  wait for PUBLISHED.
- **Expect:**  the file in 'complete/' is '.mkv', not '.avi'.  It carries tags and track statistics.

**T-40  TV_ENCODE_SD re-enables SD encoding.**

- **Start:**  'TV_ENCODE_SD=1', an SD episode.
- **Expect:**  gate 2 does not fire;  the title routes to an encoder.

## 7.  Remuxing

- **T-41  An mp4 with bin_data remuxes.**  A HandBrake mp4 carrying a QuickTime chapter stream.  Expect PUBLISHED, and the chapter count in the output equals the chapter count in the source.  Record both.
- **T-42  An avi needing genpts remuxes.**  An Xvid avi.  Expect PUBLISHED, and the packet count in equals the packet count out.  Record both.
- **T-43  The segment title is set after remux.**  A scene-named mp4.  Expect the output's segment Info title is the provider title, not the release name.

## 8.  Language and flags

- **T-44  Foreign audio is dropped.**  A file with eng and fra audio.  Expect the output holds only eng.
- **T-45  Foreign subtitles are dropped.**  A file with eng and chi subtitles.  Expect the output holds only eng.
- **T-46  Undetermined tracks are kept.**  A file with an und audio track.  Expect it survives.
- **T-47  Exactly one default audio.**  A file with two audio tracks both flagged default.  Expect the output has exactly one.  Count it, do not test that one exists.
- **T-48  No subtitle default.**  A file with a defaulted subtitle track.  Expect the output has none.
- **T-49  A forced subtitle keeps its default.**  A file with a forced subtitle flagged default.  Expect it keeps the flag.
- **T-50  A file that grew is not a failure.**  A file with PGS subtitles that grows after the strip.  Expect PUBLISHED.  Record both sizes.

## 9.  Tagging

- **T-51  A movie carries a MOVIE block.**  Expect a MOVIE-targeted Tag with TITLE, TMDB, IMDB and DATE_RELEASED.  Verify by TargetType name, never by the number.
- **T-52  Television carries three levels.**  Expect COLLECTION, SEASON and EPISODE targets, each on its own Tag element.
- **T-53  No Simple Name contains a slash.**  Parse the tag XML from any published file.  Expect no Simple whose Name contains '/'.
- **T-54  Scraped metadata survives.**  A source carrying ACTOR, DIRECTOR and GENRE.  Expect those keys present in the output.
- **T-55  Statistics are non-zero.**  For every published file, sum NUMBER_OF_BYTES and divide by the file size.  Expect a ratio above zero.  Record it.

## 10.  Encode output

- **T-56  The output codec matches the routed encoder.**  Cross-check T-31 to T-38.
- **T-57  The output is 10-bit.**  Expect pix_fmt yuv420p10le on every encoded output.
- **T-58  Colour is correct for the source.**  A tagged source keeps its properties.  An untagged NTSC DVD rip comes out smpte170m.  An HDR source keeps bt2020.  Record all three.
- **T-59  Cover art is carried, not encoded.**  A source with an mjpeg poster.  Expect the output still carries an mjpeg attachment, not a second video stream.
- **T-60  Audio and subtitles pass through unchanged.**  Expect codec and channel count identical in and out.

## 11.  Publishing

- **T-61  A movie lands in the documented shape.**  Expect 'Title (Year) [tmdbid-N] [imdbid-ttN]/Title (Year).mkv'.
- **T-62  An episode lands in the documented shape.**  Expect 'Show (Year) [tvdbid-N] [tmdbid-N]/Season NN/Show - SNNENN - Title.mkv'.
- **T-63  A duplicate does not overwrite.**  Drop the same film twice under different release names.  Expect two files, the second suffixed, and the first byte-identical to what it was.
- **T-64  Quarantine naming does not collide.**  Quarantine two files of the same name.  Expect both present, the second suffixed.
- **T-65  The source is retired.**  After PUBLISHED, expect the original file gone from import and present under 'complete/.quarantine'.

## 12.  Concurrency

**T-66  A second container waits.**

- **Start:**  one container running.
- **Do:**  start a second against the same mounts.
- **Expect:**  the second does not process anything and reports waiting.  The first keeps its lock.

**T-67  SIGTERM terminates encodes.**

- **Start:**  a container with an encode in flight.
- **Do:**  'docker stop mediaimport'.
- **Expect:**  no ffmpeg process survives.  Confirm on the host.

**T-68  An orphaned job directory is swept.**

- **Start:**  container stopped, a directory in the encode mount holding an owner file naming a dead pid.
- **Do:**  start the container.
- **Expect:**  the directory is gone.

**T-69  A live job directory is not swept.**

- **Start:**  container running with a job in flight.
- **Do:**  start a second container, let it wait, then stop the first.
- **Expect:**  the in-flight job's directory is intact when the second takes over.

## 13.  Web

- **T-70  HTTPS answers.**  'curl -sk https://localhost/api/status'.  Expect JSON.
- **T-71  Plain HTTP does not.**  'curl -s http://localhost:443/api/status'.  Expect a failure, not a redirect and not a served page.
- **T-72  The dashboard is served.**  'curl -sk https://localhost/'.  Expect HTML.
- **T-73  A held decision requeues.**  POST 'retry' against a held title.  Expect it leaves HELD.
- **T-74  An unknown action is refused.**  POST 'nonsense'.  Expect a 4xx and no state change.
- **T-75  An unknown title is 404.**  GET '/api/titles/999999'.  Expect 404.

---

## Coverage note

Router cases T-31 to T-38 are the highest risk in this plan.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails, so confirming an output file exists proves nothing.  Each router case states the expected gate and encoder and reads both back from '/api/titles/<id>', then corroborates against the output file's codec.

Four of the seven router paths are dormant at the HEVC default.  T-35 and T-36 require 'OUTPUT_CODEC=av1' and exist so those paths are exercised before that switch is ever flipped in anger.

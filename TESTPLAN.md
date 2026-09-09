# TESTPLAN

Execution plan for validating mediaImport against a built container.  Run by hand.

## What this is

This plan measures OUTCOME.  Every case asserts on something the container produced or did:  a file at a path, a filename, a tag block, a track list, an API response, an exit code, a container health state.  No case reads a log line and no case depends on a log level.

Every case is repeatable.  Each states its starting state, its input and its command, so the same case run twice from the same starting state gives the same result.

Nothing here runs on a workstation.  The workstation and the container are different environments and a result from one says nothing about the other.

## Preconditions

Before any case runs:

```
image built                 docker build -t mediaimport:local .
root mounted                MEDIA_ROOT, the one required writable mount
certificate mounted         /certs holds the certificate and key
RENDER_GID set              stat -c %g /dev/dri/renderD128 on the host

MEDIA_ENCODE and MEDIA_CONFIG are optional.  Cases that need them mounted separately say so.
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

**T-04  A missing root refuses to start.**

- **Start:**  container stopped, the MEDIA_ROOT volume removed from the compose file.
- **Do:**  'docker compose up', read the exit.
- **Expect:**  the container exits non-zero and the message names MEDIA_ROOT.

**T-04a  The optional mounts are genuinely optional.**

- **Start:**  container stopped, only MEDIA_ROOT and /certs mounted.
- **Do:**  'docker compose up -d', then read '/api/status'.
- **Expect:**  a clean start.  'encode' and 'config' are created as subdirectories of the root and the banner shows them there.

**T-04b  A retire is a rename when everything is under one root.**

- **Start:**  the collapsed layout from T-04a, a resolvable title in import.
- **Do:**  run it to CLEANUP, then compare the inode of the source before and of the quarantined file after.
- **Expect:**  the same inode.  A rename, not a copy.

**T-04c  A retire still works when encode is on another pool.**

- **Start:**  MEDIA_ENCODE mounted on separate storage.
- **Do:**  run a title to CLEANUP.
- **Expect:**  publish copies, retire renames, both succeed, and no zero-byte file is left in quarantine.

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
- **T-18a  The crop floor is 20 px.**  One source with bars between 10 and 19 px and one with bars above 20 px.  Expect the first published uncropped and reporting 'letterbox_px' 0, and the second cropped.  A 24-file library sample found the 10 to 19 px band empty, so the first file has to be constructed.

- **T-19a  The override clears a comparison hold.**  Take a title to HELD on a contradictory comparison, press Force through, and expect it to pass COMPARED with the detail naming the override and continue into the encoder rather than holding again.  Assert on the stage history.
- **T-19b  An overridden title skips the incumbent work.**  For the same run, assert no comparison object was recorded.  Its absence is what proves the gate was skipped rather than recomputed, which a verdict alone cannot show.
- **T-19c  The override does not survive a re-import.**  Drop the same source back into 'import/' afterwards and expect it gated normally on the fresh run.

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
- **Expect:**  CLEANUP, every stage logging intent, no file created anywhere, and no FAILED state.

**T-84  Discard on a missing source removes the row.**

- **Start:**  a held title whose source file has been renamed away.
- **Do:**  POST 'discard' against it.
- **Expect:**  the row is gone from '/api/titles', nothing written to quarantine, and the response action reads "forgotten".

## 5.  Comparison against the incumbent

Each case needs a prepared library file and a prepared incoming file that differ on exactly one gate.

- **T-23  Gate 1, losing HDR is a loss.**  Incumbent HDR, incoming SDR.  Expect QUARANTINED, incoming file present under 'complete/.quarantine'.
- **T-24  Gate 2, higher resolution wins.**  Incoming 2160p against 1080p incumbent.  Expect it proceeds.
- **T-25  Gate 3, larger picture area wins.**  Incoming without bars against a letterboxed incumbent.  Expect it proceeds.
- **T-25a  Gate 2 defers when either side carries bars.**  A correctly cropped 1920x800 incoming file against an incumbent stored 1920x1080 with 280 px of bars and a greater bit depth.  Expect QUARANTINED decided at gate 5 on bit depth, the comparison notes carrying the gate 2 defer, and the table showing 'letterbox_px' 0 against 280.  This is the Blade Runner 2049 pair from 2026-09-08.
- **T-25b  Gate 2 still decides when neither side carries bars.**  A 2160p incoming file against a 1080p incumbent, neither letterboxed.  Expect the verdict at gate 2 and no defer note.  Regression guard on T-25a.
- **T-25d  A contradictory pair holds instead of deciding.**  An incoming file better on bitrate and worse on bit depth.  Expect HELD, the comparison verdict 'ambiguous' with no single gate, and the votes list naming gate 5 for the incumbent and gate 6 for the incoming.  Expect nothing in quarantine.
- **T-25e  A unanimous pair still decides.**  An incoming file better on two gates and worse on none.  Expect the verdict recorded, the reason carrying the agreeing-gate count, and every voting row marked in the table.
- **T-25c  An equivalent pair behind bars holds rather than quarantining.**  The T-25a pair with both sides at the same bit depth.  Expect HELD as ambiguous, not QUARANTINED.  Before the defer this pair was discarded on black bars alone.
- **T-26  Gate 4, more audio channels wins.**  Incoming 5.1 against 2.0.  Expect it proceeds.
- **T-27  Gate 5, greater bit depth wins.**  Incoming 10-bit against 8-bit.  Expect it proceeds.
- **T-28  Gate 6, better pedigree breaks a tie.**  Identical but for release naming.  Expect it proceeds and the comparison notes record the weak signal.
- **T-29  A level pair holds.**  Two files identical on every gate.  Expect HELD, and '/api/titles/<id>' carries a comparison object.
- **T-29a  The incumbent is found by provider ID when the folder name has drifted.**  A library folder carrying the right '[tmdbid-N]' whose name does not match the current transform, for example an en dash title filed with no dash at all.  Expect the comparison to run and the COMPARED detail to name the ID route.  This is the Return of the Jedi case from 2026-09-08.
- **T-29b  The name fallback still works and is labelled.**  A library folder whose name matches the transform exactly and whose provider IDs are absent from the folder name.  Expect the comparison to run and the COMPARED detail to name the folder-name route.
- **T-29d  Gate 6 reads the BPS tag.**  Expect 'video_bitrate' populated on both sides of the comparison table, and the figure to match the video track's BPS tag read directly with ffprobe.
- **T-29e  Codec weighting applies.**  An HEVC incumbent against an h264 arrival whose raw bitrate is below it but above it once weighted at 1.7.  Expect gate 6 to vote for the arrival.
- **T-29f  An unmeasurable bitrate casts no vote.**  A source carrying neither a BPS tag nor NUMBER_OF_BYTES.  Expect gate 6 skipped with a note, and the verdict decided by the remaining gates rather than held.
- **T-29c  A genuine miss is distinguishable from a lookup failure.**  A title with no library counterpart.  Expect the COMPARED detail to report the number of folders scanned rather than a bare 'no incumbent' sentence.

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
- **T-58  Colour is correct for the source.**  Read the properties off the OUTPUT FILE, never off the command that produced it.  Four sources through the x265 path:  a tagged source keeps its own properties;  an untagged SD NTSC rip comes out smpte170m;  an untagged HD source comes out bt709;  an HDR source keeps bt2020.  Record all four.
- **T-58a  The colour mechanism differs by path and must not be generalised.**  Run the untagged HD source and the untagged SD rip through both AV1 paths.  Expect the same four outcomes as T-58, produced by the three ffmpeg colour flags rather than by the params string.  The x265 path must still pass none of those three flags.
- **T-59  Cover art is carried, not encoded.**  A source with an mjpeg poster.  Expect the output still carries an mjpeg attachment, not a second video stream.
- **T-60  Audio and subtitles pass through unchanged.**  Expect codec and channel count identical in and out.

- **T-60a  The published file carries a targeted tag block.**  Assert structurally on the file in 'complete/', not by grep:  every expected TargetType appears on its own Tag element, and no Simple Name anywhere contains a slash.  Run one movie and one episode.
- **T-60b  Scraped metadata survives the encode.**  A source carrying an untargeted block of ACTOR, DIRECTOR, GENRE and SYNOPSIS.  Expect every one of those keys present on the published file.
- **T-60c  Statistics survive the post-encode tag write.**  Record the byte-sum ratio on the published file.  Expect it above the floor and not zero.
- **T-60d  Video packets are preserved through the encode.**  Expect the VERIFIED stage detail on '/api/titles/<id>' to carry a video packet figure equal to the source count.  Measured 2026-09-08:  an 89 minute encode preserved 128,424 packets exactly, so this is an equality and not a tolerance.  Then truncate an encoder output by hand before verification and expect the title HELD naming the packet mismatch rather than published.

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

## 12a.  Comparison measurement and the state record

- **T-70a  A speed-up is reported even though no gate acts on it.**  An incumbent that is a 25 fps speed-up of the same master as the incoming file.  Expect the comparison notes to name the differing frame rates at equal frame counts, and the UI row for frame rate to be marked as a difference no gate acted on.
- **T-70b  Gate 3 fires with real numbers.**  An incumbent that is genuinely letterboxed against a correctly cropped incoming file.  Expect picture pixels populated on BOTH sides and the gate to decide, rather than the 'cropdetect not run on both sides' note.
- **T-70c  The published output is the third column.**  Open the Compare dialog on a completed title.  Expect a Published column whose values match what ffprobe reports on the file in 'complete/'.
- **T-70d  The dialog renders untrusted text as text.**  A source filename containing '&', '<' and a double quote.  Expect the characters displayed literally and no broken markup.
- **T-70e  A removed file leaves a record that can be cleared.**  Take a title to CLEANUP, collect its published output out of 'complete/' by hand, delete its quarantined source by hand, and refresh.  All three of output, source and quarantine copy must be absent before the row counts as gone.  Expect the row marked 'files gone' and a Forget button.  Press it and expect the row to disappear.  Confirm no file was deleted by the container.
- **T-70i  A quarantined title is not reported as gone.**  Quarantine a title through a comparison loss.  Expect the row marked 'files quarantined' rather than 'files gone', no Forget button, the row not dimmed, and a forget POST refused with the message naming files still on disk.  Confirm the file is present under 'complete/.quarantine' at its full size.
- **T-70f  A re-imported source is processed, not skipped.**  After T-70e, drop the same source back into 'import/' under the same filename.  Expect it detected and processed rather than silently ignored.
- **T-70g  A source still in flight is not processed twice.**  While a title is at ENCODING, confirm '/api/titles' holds no second row for its path and that the original row keeps its stage.  The skip itself is a debug line and is not asserted on.
- **T-70h  The log records the encode and not the polls.**  Run one title to CLEANUP at the default 'info' level with a second title in flight.  Expect one ENCODED line and one VERIFIED line, each naming the title id, and no 'already claims this path' line at info.  This is the one case that reads the log, and it exists because the defect it guards was the log itself.

## 13.  Web

- **T-70  HTTPS answers.**  'curl -sk https://localhost/api/status'.  Expect JSON.
- **T-71  Plain HTTP does not.**  'curl -s http://localhost:443/api/status'.  Expect a failure, not a redirect and not a served page.
- **T-72  The dashboard is served.**  'curl -sk https://localhost/'.  Expect HTML.
- **T-76  A poster appears once a title is identified.**  Import a film with a resolvable tmdb id.  Expect '/api/titles/<id>' to carry a 'poster_url' after IDENTIFIED, and 'GET /api/poster/<id>' to return image bytes with an image content type.
- **T-77  A title with no poster falls back to a text tile.**  A file held at the standards gate, which never identifies.  Expect 'poster_url' null, '/api/poster/<id>' to answer 404, and the tile to show the filename on the same footprint rather than a blank.
- **T-78  Posters are served from the container, not from TMDB.**  After a poster has been fetched once, confirm a file exists under 'config/cache/posters', then block outbound internet and reload the dashboard.  Expect the poster still rendered.
- **T-79  A poster fetch never blocks identification.**  While a dashboard with uncached posters is loading, confirm a title still advances through IDENTIFIED at the normal rate.  The poster path must not sit behind the 3 second provider throttle.
- **T-80  Television collapses to one tile per show.**  Import a season.  Expect one tile carrying the series poster and the episode count, the box header counting titles rather than tiles, and clicking the tile to list the episodes.
- **T-81  A held title is actionable from its tile.**  Click a held tile.  Expect the detail dialog with Compare, Retry, Force through and Discard, and each button to reach '/api/held/<id>/decision'.
- **T-82  The counters are clickable and independent.**  With titles in both states, expect 'Quarantined Files' and 'Failed Jobs' to open separate lists, each showing the reason per title and a Compare button where a comparison exists.
- **T-84  A TVDB-only show gets artwork.**  Import a season of a show that resolves tvdb but not tmdb.  Expect 'poster_url' populated from artworks.thetvdb.com and the group tile to render it.
- **T-85  A season resolves its slug once.**  Time a fresh season import against a single-episode one.  Expect the difference to be the per-episode work only;  a season paying an extra 3 seconds per episode means the poster memo is not holding.
- **T-86  The group tile names the show.**  A television group with no artwork available.  Expect its placeholder to read the show name, not one episode's filename.
- **T-87  The episode list acts at both levels.**  Open a held season.  Expect per-row Retry, Force through and Discard, plus header actions for all of them, and expect a header action to close the list and move every episode back to Queue.
- **T-88  Quarantined rows offer no decisions.**  Open the Quarantined list.  Expect Details and Compare only, since the decision endpoint rejects a title that is not awaiting one.
- **T-89  Scraped titles carry no HTML entities.**  Import an episode whose provider title contains an apostrophe or an accent.  Expect the stored title decoded, for example "Let's Give the Boy a Hand" and not 'Let&#039;s Give the Boy a Hand', and the same on the published filename and the Matroska EPISODE tag.
- **T-90  An entity title matches exactly, not fuzzily.**  For that same episode, expect the match method to be exact.  A fuzzy match means the decode did not happen:  one entity still scores about 0.86 and passes silently, which is the failure this case exists to catch.
- **T-91  Detection lands inside the new window.**  Time a copy into 'import/' from completion to the 'detected' line.  Expect 30 to 45 seconds.  Repeat with a large file, where a slow write would show as a premature detection.
- **T-92  A partial copy is still refused.**  Interrupt a copy mid-transfer and leave it untouched past MTIME_QUIET.  Expect it detected and then held by ffprobe or the minimum standards gate, never encoded.
- **T-93  The queue holds three tiles per row.**  At 1920x1080 with the queue populated, expect three tiles across in Queue and two in every other box, with no page scrollbar.
- **T-83  The layout fits 1920x1080.**  Load the dashboard at that size with every box populated.  Expect no page scrollbar, and each box to scroll internally instead.
- **T-73  A held decision requeues.**  POST 'retry' against a held title.  Expect it leaves HELD.
- **T-74  An unknown action is refused.**  POST 'nonsense'.  Expect a 4xx and no state change.
- **T-75  An unknown title is 404.**  GET '/api/titles/999999'.  Expect 404.

---

## Coverage note

Router cases T-31 to T-38 are the highest risk in this plan.  A misrouted title produces a valid file with the wrong tradeoff and nothing fails, so confirming an output file exists proves nothing.  Each router case states the expected gate and encoder and reads both back from '/api/titles/<id>', then corroborates against the output file's codec.

Four of the seven router paths are dormant at the HEVC default.  T-35 and T-36 require 'OUTPUT_CODEC=av1' and exist so those paths are exercised before that switch is ever flipped in anger.

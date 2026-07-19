# Pelody

An homage to *Pel* (scriptedfun, Armor Games 2008): a paddle on 3 lanes juggling glowing
balls — except here **the balls play the song**. Every catch sounds the next melody note
exactly on the beat. Misses just mute a note; the music never stops.

Songs: Korobeiniki (builtin transcription) plus Mountain King, Clair de Lune, Dearly
Beloved, and Yann Tiersen's "Comptine d'un autre été" and "La Valse d'Amélie", compiled
from real MIDIs in `midi/`. The melody line rides the balls; **every other note of the
arrangement plays as synced backing** (same heard-time clock plus a rolling 20-second
prebake, so it can't drift).

Open `index.html` in a browser (it's fully self-contained, works from `file://`).

- **←/→** step a lane · **A S D** jump straight to a lane · tap left/mid/right thirds on mobile
- **Esc/Space** pause/resume · **R** restart the current song from the count-in
- **N** next song · **B** ball model: JUGGLE (fewer, persistent balls) vs STREAM (more,
  transient balls in tall carousel arcs, Pel-style enter/exit)
- **P** autopilot — the paddle plays perfectly so you can watch the choreography; any
  manual input takes over
- **G** diagnostics overlay (heard-time clock, latency, catch stats)
- URL params: `?av=<ms>` audio/visual offset, `?input=<ms>` input offset

Design notes: throws are true parabolas under ONE gravity (same airtime = same height, full
screen at ~2.1 s; very high throws sail off the top and fall back in — same ball, same
trail). The paddle is a convex elliptical arc (same ±44° normal fan as a circle at 25%
less bulge) and every ball lands at the geometrically correct contact point — where the face normal bisects the
incoming and outgoing flight directions (specular reflection). Choreography is compiled
offline: ball chains are a minimum path cover of the legal-succession DAG (max bipartite
matching + 2-opt), so juggle mode keeps each ball in play as long as possible; close notes
always on different balls, fast lane changes only off telegraphed high arcs. Audio runs on
a "heard-time" clock derived from `AudioContext.getOutputTimestamp()`.

Regenerating songs: `python3 midi_to_song.py --check --write` rebuilds the choreography and
injects it into `index.html` (refuses to write if validation fails). Any `.mid` dropped into
`midi/` is compiled in automatically (or one-off: `--mid file.mid --title "Name"`). Reference
research lives in the repo's WebGlBeats clone and the decompiled original Pel in the session
scratchpad.

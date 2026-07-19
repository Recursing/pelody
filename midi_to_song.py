#!/usr/bin/env python3
"""midi_to_song.py — compile songs into Pelody choreography.

Usage:
  python3 midi_to_song.py --write            # rebuild builtin songs into index.html
  python3 midi_to_song.py --check            # validate choreography, print stats
  python3 midi_to_song.py --mid f.mid --title "Name" --write   # add a real MIDI
  optional with --mid: --melody-track N

Pipeline: notes (beat domain) -> tempo map (beat->seconds) -> quantize/thin ->
cascade choreography (lanes by rotation; threads oldest-idle-first with
no-fast-reversal rule) -> chains (entry/exit) -> compact JS injected between
/*SONGS_START*/ and /*SONGS_END*/ in index.html.

Design rules (Lorenzo): cascades first — every ball circulates one direction;
a ball returning to the lane it came from (X->Y->X) is rare and NEVER fast
(return airtime >= 3 beats). Close notes always ride different balls.
"""
import argparse, json, math, struct, sys
from bisect import bisect_left
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
INDEX = HERE / "index.html"

# ---------------- tempo map ----------------
class TempoMap:
    """Piecewise tempo: list of (beat, spb). Seconds via cumulative sum."""
    def __init__(self, segs):
        self.segs = sorted(segs)
        self._starts = [0.0]
        for i in range(1, len(self.segs)):
            b0, spb = self.segs[i-1]
            b1, _ = self.segs[i]
            self._starts.append(self._starts[-1] + (b1-b0)*spb)
    def sec(self, beat):
        for i in range(len(self.segs)-1, -1, -1):
            b0, spb = self.segs[i]
            if beat >= b0:
                return self._starts[i] + (beat-b0)*spb
        return beat*self.segs[0][1]
    def spb_at(self, beat):
        for i in range(len(self.segs)-1, -1, -1):
            if beat >= self.segs[i][0]:
                return self.segs[i][1]
        return self.segs[0][1]

# ---------------- builtin transcriptions ----------------
def korobeiniki():
    A = [  # (beat, midi, len_beats) — 8 bars
        (0,76,1),(1,71,.5),(1.5,72,.5),(2,74,1),(3,72,.5),(3.5,71,.5),
        (4,69,1),(5,69,.5),(5.5,72,.5),(6,76,1),(7,74,.5),(7.5,72,.5),
        (8,71,1.5),(9.5,72,.5),(10,74,1),(11,76,1),
        (12,72,1),(13,69,1),(14,69,1),
        (16.5,74,1),(17.5,77,.5),(18,81,1),(19,79,.5),(19.5,77,.5),
        (20,76,1.5),(21.5,72,.5),(22,76,1),(23,74,.5),(23.5,72,.5),
        (24,71,1),(25,71,.5),(25.5,72,.5),(26,74,1),(27,76,1),
        (28,72,1),(29,69,1),(30,69,1),
    ]
    B = [  # 8 bars, half-time chorale
        (0,76,2),(2,72,2),(4,74,2),(6,71,2),
        (8,72,2),(10,69,2),(12,68,2),(14,71,2),
        (16,76,2),(18,72,2),(20,74,2),(22,71,2),
        (24,72,1),(25,76,1),(26,81,2),(28,80,4),
    ]
    mel = [(b,m,l) for b,m,l in A] + [(b+32,m,l) for b,m,l in A] \
        + [(b+64,m,l) for b,m,l in B] + [(b+96,m,l) for b,m,l in B]
    rootsA = [45,45,40,45,38,45,40,45]
    rootsB = [40,40,45,44,40,40,45,52-12]
    back = []   # oom-pah: root on 1/3, power-chord stab on 2/4 (no wrong thirds)
    for loop, roots, off in ((0,rootsA,0),(1,rootsA,32),(2,rootsB,64),(3,rootsB,96)):
        for bar in range(8):
            r = roots[bar]
            for k in (0,2): back.append((off+bar*4+k, r, .5, 1.0))
            for k in (1,3):
                back.append((off+bar*4+k, r+12, .3, .5))
                back.append((off+bar*4+k, r+19, .3, .5))
    notes = [dict(beat=b, midis=[m], vel=.85 if l>=1 else .65) for b,m,l in mel]
    return dict(name="korobeiniki", title="Korobeiniki",
                tempo=TempoMap([(0, 60/116)]), beats_len=128,
                melody=notes, back=back, perc=[], flip_at=64)

# Everything except Korobeiniki compiles from real MIDIs in midi/ (the old
# hand transcriptions were missing most of the arrangement).
SONG_CFG = {   # per-file options: title, speed (playback multiplier),
               # drums=True (synthesize density-gated kit)
    "mountain_king": dict(title="In the Hall of the Mountain King", melody="active",
                          min_event=.09,    # let the fast theme ride balls: hard but predictable
                          licks=[(2,1,2,2,-4,4),    # the famous ascending head, pinned
                                 (2,2,1,2,-3,3),    # ...its major-mode statement
                                 (0,2,0,1,0,2),     # ...doubled-eighths head, minor
                                 (0,2,0,2,0,1)]),   # ...doubled-eighths head, major
    "dearly_beloved": dict(title="Dearly Beloved", speed=1.15),
    "clair_de_lune": dict(title="Clair de Lune", melody="sustain"),
    # Clean two-track piano arrangement: keep the right-hand theme on balls
    # and leave the left hand (plus unselected RH chord tones) in backing.
    "fairytale": dict(title="Fairytale (Shrek)", mel_track=0),
    "comptine": dict(title="Comptine d'un autre été"),
    # solo piano waltz: RH holds the tune while playing an inner E/F murmur
    # and the LH waltz chords hit under held tune notes — pin melody to the
    # RH track (kills LH leaks) and let "sustain" demote the inner murmur
    "valse_amelie": dict(title="La Valse d'Amélie", mel_track=0, melody="sustain"),
}

# ---------------- real MIDI parsing (no deps) ----------------
def read_varlen(data, i):
    v = 0
    while True:
        b = data[i]; i += 1; v = (v << 7) | (b & 0x7F)
        if not b & 0x80: return v, i

def parse_midi(path):
    data = Path(path).read_bytes()
    assert data[:4] == b"MThd", "not a MIDI file"
    _, fmt, ntrk, div = struct.unpack(">IHHH", data[4:14])
    assert fmt in (0, 1), f"MIDI format {fmt} not supported (independent track timelines)"
    assert not div & 0x8000, "SMPTE time not supported"
    i, tracks, tempo_evs = 14, [], []
    for _ in range(ntrk):
        assert data[i:i+4] == b"MTrk"
        ln = struct.unpack(">I", data[i+4:i+8])[0]
        end = i + 8 + ln; j = i + 8; tick = 0; run = 0
        notes, on = [], {}
        while j < end:
            d, j = read_varlen(data, j); tick += d
            st = data[j]
            if st & 0x80: j += 1; run = st
            else: st = run
            typ = st & 0xF0
            if st == 0xFF:
                meta = data[j]; ln2, j2 = read_varlen(data, j+1); j = j2 + ln2
                if meta == 0x51:
                    us = int.from_bytes(data[j-ln2:j], "big")
                    tempo_evs.append((tick, us/1e6))
            elif st == 0xF0 or st == 0xF7:
                ln2, j2 = read_varlen(data, j); j = j2 + ln2
            elif typ in (0x80, 0x90):
                p, v = data[j], data[j+1]; j += 2
                ch = st & 0x0F
                if typ == 0x90 and v > 0: on[(ch,p)] = (tick, v)
                elif (ch,p) in on:
                    t0, v0 = on.pop((ch,p))
                    notes.append(dict(tick=t0, midi=p, vel=v0/127, dur=tick-t0, ch=ch))
            elif typ in (0xA0, 0xB0, 0xE0): j += 2
            elif typ in (0xC0, 0xD0): j += 1
            else: raise ValueError(f"bad status {st:#x}")
        tracks.append(notes); i = end
    segs = [(t/div, spq) for t, spq in sorted(tempo_evs)] or [(0.0, 0.5)]
    if segs[0][0] > 0: segs.insert(0, (0.0, 0.5))   # SMF default 120 BPM before the first marker
    return tracks, TempoMap(segs), div

def song_from_midi(path, title=None, mel_track=None):
    """Melody (top line) rides the balls; EVERY other note of the file becomes
    synced backing; ch9 becomes percussion. Some exports duplicate the whole
    mix across tracks — dedupe by (tick, pitch) first."""
    stem = Path(path).stem
    cfg = SONG_CFG.get(stem, {})
    if mel_track is None: mel_track = cfg.get("mel_track")
    tracks, tempo, div = parse_midi(path)
    if cfg.get("speed"):
        tempo = TempoMap([(b, spb/cfg["speed"]) for b, spb in tempo.segs])
    notes = [dict(n, trk=ti) for ti, t in enumerate(tracks) for n in t]
    perc = [(n["tick"]/div, n["midi"]) for n in notes if n["ch"] == 9]
    # timpani sequenced as sub-audible low keys on a melodic channel -> booms
    perc += [(n["tick"]/div, 36) for n in notes if n["ch"] != 9 and n["midi"] < 24]
    pitched = [n for n in notes if n["ch"] != 9 and n["midi"] >= 24]
    assert pitched, "no melodic notes found"
    # melody: the lead voice (highest mean pitch among substantial groups),
    # but during its long rests the tune lives elsewhere (e.g. an intro
    # statement in another instrument) — borrow the top line there
    if mel_track is not None:
        mel_notes = [n for n in pitched if n["trk"] == mel_track]
    elif cfg.get("melody") == "active":
        # orchestral scores pass the theme around (cello pizz -> violins ->
        # winds): per 4-beat window, the busiest MOVING line is the tune.
        # Raw onset count alone crowns timpani rolls and 2-note accompaniment
        # figures — weight by pitch variety, and keep the previous carrier on
        # near-ties so phrases don't flip voices mid-thought.
        wins = {}
        for n in pitched:
            wins.setdefault(int(n["tick"]/div/4), {}) \
                .setdefault((n["trk"], n["ch"]), []).append(n)
        # the tune is the busiest voice that isn't texture. Texture tells:
        # trills/rolls/octave-pedals repeat 1-2 pitch CLASSES; decorations
        # LEAP (arpeggios) while melodies mostly STEP. Score the top-note-
        # per-tick line: gate texture, then onsets x stepwise-motion bonus.
        def score(ns):
            byt = {}
            for x in ns: byt[x["tick"]] = max(byt.get(x["tick"], 0), x["midi"])
            line = [m for _, m in sorted(byt.items())]
            c = Counter(line)
            top2 = sum(k for _, k in c.most_common(2))
            if len({m % 12 for m in line}) < 3 or top2/len(line) > .8:
                return len(line)*.1
            steps = [abs(b-a) for a, b in zip(line, line[1:]) if b != a]
            mean = sum(steps)/len(steps) if steps else 6
            return len(line)*max(.5, min(1.5, 2.5/mean))
        # THE famous theme = the most-repeated 6-interval lick in the whole
        # piece (transposition-invariant). Any voice playing it in a window
        # wins that window outright; activity scoring is only the fallback.
        def topline(ns):
            byt = {}
            for x in ns: byt[x["tick"]] = max(byt.get(x["tick"], 0), x["midi"])
            return sorted(byt.items())
        voices = {}
        for w in wins.values():
            for g, ns in w.items(): voices.setdefault(g, []).extend(ns)
        licks = Counter()
        for g, ns in voices.items():
            line = [m for _, m in topline(ns)]
            for i in range(len(line)-6):
                seg = line[i:i+7]
                iv = tuple(seg[j+1]-seg[j] for j in range(6))
                # a theme TRAVELS; trills/pedals hover (span < a fourth)
                if any(iv) and max(seg)-min(seg) >= 5: licks[iv] += 1
        themes = {sig for sig, c in licks.most_common(8) if c >= 8}
        themes |= {tuple(l) for l in cfg.get("licks", [])}   # user-pinned themes
        allv = {}
        for w, d in wins.items():
            for g, ns in d.items():
                allv.setdefault(g, {})[w] = ns
        def hits(g, w):
            # licks STARTING in window w; neighbor windows give line context
            ns = allv[g].get(w-1, []) + allv[g].get(w, []) + allv[g].get(w+1, [])
            tl = topline(ns); line = [m for _, m in tl]
            h = 0
            for i in range(len(line)-6):
                if tuple(line[i+j+1]-line[i+j] for j in range(6)) in themes \
                        and w*4 <= tl[i][0]/div < (w+1)*4:
                    h += 1
            return h
        # windows are too coarse: a statement = 4-beat head lick + 4-beat tail,
        # and the tail alone matches no lick, so trills outscore it. RANGE
        # CLAIMING: each lick occurrence claims its voice's line from the lick
        # onset for as long as the voice keeps sounding (<=8 beats, padded to
        # the window edge). Pinned licks in cfg order (famous head first)
        # outrank auto-detected ones (e.g. the looping bass answer, which
        # chain-matches every start and out-hits any coexisting head); the
        # best-ranked claim wins each quarter-beat slot. Only unclaimed time
        # falls back to the per-window activity pick.
        rank = {tuple(l): r for r, l in enumerate(cfg.get("licks", []))}
        claims = []
        for g, ns in voices.items():
            tl = topline(ns); line = [m for _, m in tl]
            for i in range(len(line)-6):
                sig = tuple(line[i+j+1]-line[i+j] for j in range(6))
                if sig not in themes: continue
                k = i+6
                while k+1 < len(tl) and tl[k+1][0]-tl[k][0] <= 2*div \
                        and tl[k+1][0] < tl[i][0]+8*div: k += 1
                # octave doublings tie: prefer the voice already in pluck
                # range, else the whole phrase gets folded down later
                claims.append((rank.get(sig, 9), tl[i][0],
                               max(line[i:k+1]) > 91, tl[k][0], g))
        owner = {}
        for _, t0, _, t1, g in sorted(claims):
            for q in range(int(t0/div*4), (int(t1/div/4)+1)*16):
                owner.setdefault(q, g)
        def own(n): return owner.get(int(n["tick"]/div*4))
        mel_notes = [n for n in pitched if own(n) == (n["trk"], n["ch"])]
        prev = None
        for w in sorted(wins):
            d = wins[w]
            best = max(d, key=lambda g: (hits(g, w), score(d[g])))
            if prev in d and prev != best \
                    and hits(prev, w) == hits(best, w) \
                    and score(d[prev]) >= .6*score(d[best]): best = prev
            mel_notes += [n for n in d[best] if own(n) is None]; prev = best
    else:
        groups = {}
        for n in pitched: groups.setdefault((n["trk"], n["ch"]), []).append(n)
        big = max(len(g) for g in groups.values())
        cands = [g for g in groups.values() if len(g) >= big*.25]
        mel_notes = max(cands, key=lambda g: sum(n["midi"] for n in g)/len(g))
        ticks = sorted({n["tick"] for n in mel_notes})
        ids = {id(n) for n in mel_notes}
        def near(tk):
            i = bisect_left(ticks, tk)
            return min([abs(ticks[j]-tk) for j in (i-1, i) if 0 <= j < len(ticks)] or [1e18])
        mel_notes = mel_notes + [n for n in pitched
                                 if id(n) not in ids and near(n["tick"]) > 1.5*div]
    if cfg.get("mel_floor"):
        # single-track EDM exports merge bass and lead into one voice; when
        # the lead rests, the top-note-per-slot rule would put the BASS on a
        # ball mid-riff — keep everything under the floor as backing instead
        mel_notes = [n for n in mel_notes if n["midi"] >= cfg["mel_floor"]]
    if cfg.get("melody") == "sustain":
        # solo piano: voice-selection can't help — melody and accompaniment
        # share one voice. There the tune is the SUSTAINED line: held notes
        # (>= 1 beat) carry it while fast arpeggios ripple around and peak
        # above it (worse, the close-pair vel rule below lets loud arp 16ths
        # evict a quiet held tune note). A note starting while an earlier,
        # HIGHER note still rings is decoration -> backing: brief notes
        # (< 1 beat) lose unless the ring ends within half a beat of their
        # onset (a phrase tail must not eat the next phrase's pickup);
        # longer notes lose only if the ringing note outlasts them (bass
        # pedals under a held tune note).
        longs = [n for n in mel_notes if n["dur"] >= div]
        def deco(n):
            need = n["tick"] + (div//2 if n["dur"] < div else n["dur"])
            return any(L["tick"] < n["tick"] and L["midi"] > n["midi"]
                       and L["tick"]+L["dur"] >= need for L in longs)
        mel_notes = [n for n in mel_notes if not deco(n)]
    # top note per 1/4-beat slot, then >=0.4-beat spacing (quieter of a close
    # pair loses); everything not picked stays audible as backing
    slots = {}
    for n in mel_notes:
        q = round(n["tick"]/div*4)/4
        slots.setdefault(q, []).append(n)
    picked = []
    for b, ns in sorted(slots.items()):
        n = max(ns, key=lambda x: x["midi"])
        if picked and b-picked[-1][0] < .4:
            if n["vel"] > picked[-1][1]["vel"]: picked[-1] = (b, n, ns)
        else: picked.append((b, n, ns))
    chosen = {id(n) for _, n, _ in picked}
    melody = []
    for i, (b, n, ns) in enumerate(picked):
        melody.append(dict(beat=b, midis=[n["midi"]], vel=.5+.5*n["vel"]))
        # chords: at calm moments (>=1 beat of space on both sides) a second
        # ball rides in and lands the SAME lane at the SAME instant
        calm = (b-picked[i-1][0] >= 1 if i else True) and \
               (picked[i+1][0]-b >= 1 if i+1 < len(picked) else True)
        low = [x for x in ns if x["midi"] < n["midi"] and n["midi"]-x["midi"] <= 12]
        if calm and low:
            x = max(low, key=lambda x: x["midi"])
            chosen.add(id(x))
            melody.append(dict(beat=b, midis=[x["midi"]],
                               vel=(.5+.5*x["vel"])*.85, chord=True))
    # fold whole PHRASES into pluck range: per-note folding slices a high
    # run mid-contour (some notes drop an octave, neighbors don't) and
    # sounds broken — shift each phrase uniformly instead
    phrase = []
    for e in melody + [None]:
        if phrase and (e is None or e["beat"]-phrase[-1]["beat"] > 2):
            drop = 0
            top = max(m for p in phrase for m in p["midis"])
            while top+drop > 91: drop -= 12
            if drop:
                for p in phrase: p["midis"] = [m+drop for m in p["midis"]]
            phrase = []
        if e is not None: phrase.append(e)
    # backing = everything not ridden by a ball, deduped: unison doublings
    # across instruments (and dup-mix exports) collapse to one voice
    mel_keys = {(n["tick"], n["midi"]) for n in pitched if id(n) in chosen}
    seen, back = set(), []
    for n in pitched:
        key = (n["tick"], n["midi"])
        if id(n) in chosen or key in seen or key in mel_keys: continue
        seen.add(key)
        back.append((n["tick"]/div, n["midi"], .25+.45*n["vel"], max(.1, n["dur"]/div)))
    if cfg.get("drums"):   # density-gated: four-on-the-floor in builds/drops only
        import collections
        dens = collections.Counter(int(n["tick"]/div) for n in pitched)
        for b in range(max(dens)+1):
            if dens.get(b, 0) >= 7:
                perc.append((b, 36))
                if b % 4 in (1, 3): perc.append((b, 38))
            if dens.get(b, 0) >= 6: perc.append((b+.5, 42))
    back.sort(); perc.sort()
    end = max(m["beat"] for m in melody) + 4
    return dict(name=stem, title=title or cfg.get("title") or stem,
                tempo=tempo, beats_len=end, melody=melody, back=back, perc=perc,
                min_event=cfg.get("min_event", MIN_EVENT_SEC), flip_at=end/2)

# ---------------- cascade choreography ----------------
MIN_GAP_PREF, MIN_GAP_HARD, MAX_AIR, REV_MIN = 1.2, .6, 5.0, 2.5   # REV_MIN == TELEGRAPH: a 2.5-beat arc reads as slow/visible
MIN_MOVE_SEC = .28   # faster than this, the paddle shouldn't have to change lane
MIN_EVENT_SEC = .15  # faster than this, off-beat notes become ghosts
TELEGRAPH = 2.5      # a fast lane-change is fair only off a ball this long in the air (beats)
FAST_CD_SEC = 3.0    # ...and not more often than this (and never twice in a row)
GRP_BEATS = 2.0      # the cascade steps by GROUPS: the paddle rests about this
                     # long per lane (a few notes) before the pattern moves on
HARD_AIR = 8.0       # ...but a ball may float this long rather than land ONCE and leave
SELF_SPLIT_SEC = 1.5 # a ball about to self-bounce faster than this exits and a
                     # fresh ball takes the note (runs stream instead of pogoing)
MODES = {  # per-mode choreography params (more balls = taller, slower, easier arcs)
    # Airtime ceilings are in SECONDS: gravity is one global constant now
    # (apex = g/8*T^2), so airtime alone sets height, regardless of tempo.
    # 6s = two clean 3s legs of the off-screen fourth-column bounce.
    # chain_cap: max catches per ball (None = a ball lives while it can).
    # JUGGLE keeps balls in play as long as possible; STREAM is transient
    # Pel-style balls.
    "juggle": dict(K=7, max_air_sec=5.0, hard_air_sec=6.0, chain_cap=None),
    "stream": dict(K=7, max_air_sec=3.0, hard_air_sec=4.5, chain_cap=12),
}

def thin(song):
    """Only truly extreme density gets demoted to ghosts (auto-played, no
    ball); merely-fast runs stay catchable — they park in one lane instead.
    When two notes are closer than the hard floor, the on-beat one wins."""
    t = song["tempo"]; min_ev = song.get("min_event", MIN_EVENT_SEC)
    playable, ghosts = [], []
    for e in song["melody"]:
        if e.get("chord"): playable.append(e); continue   # rides its top note
        sec = t.sec(e["beat"])
        while playable and sec-t.sec(playable[-1]["beat"]) < min_ev \
                and e["beat"] % 1 == 0 and playable[-1]["beat"] % 1:
            ghosts.append(playable.pop())
        if playable and sec-t.sec(playable[-1]["beat"]) < min_ev:
            ghosts.append(e)
        else:
            playable.append(e)
    ghosts.sort(key=lambda g: g["beat"])
    return playable, ghosts

def choreograph(song, K, melody, max_air_sec=3.0, hard_air_sec=4.5, chain_cap=12):
    ev = [dict(e) for e in melody]
    tempo = song["tempo"]
    flip = song["flip_at"]
    seq_fwd, seq_rev = [0,1,2], [2,1,0]
    last_beat = [-1e9]*K; last_lane = [None]*K; prev_lane = [None]*K
    air = [None]*K   # each thread's previous airtime -> prefer repeating it
    j = 0; grp_start = -1e9; last_sec = -1e9; last_ev_lane = None
    last_fast_sec = -1e9; prev_was_fast = False
    for e in ev:
        sec = tempo.sec(e["beat"])
        max_air = max_air_sec/tempo.spb_at(e["beat"])   # seconds -> beats, here
        seq = seq_rev if (flip is not None and e["beat"] >= flip) else seq_fwd
        fast = sec-last_sec < MIN_MOVE_SEC and last_ev_lane is not None
        forced_pick = None
        if e.get("chord") and last_ev_lane is not None and sec == last_sec:
            e["lane"] = last_ev_lane          # lands WITH its top note
        elif fast:
            # occasionally allow the change anyway — but ONLY landed by a
            # telegraphed ball (a thread long in the air, or a fresh entry),
            # rate-limited, and never two fast changes in a row
            cand = -1
            nxt = seq[(j+1) % 3]
            if not prev_was_fast and sec-last_fast_sec >= FAST_CD_SEC \
                    and e["beat"]-grp_start >= GRP_BEATS and nxt != last_ev_lane:
                best_t = 1e9
                for k in range(K):
                    gap = e["beat"]-last_beat[k]
                    rev = prev_lane[k] is not None and prev_lane[k] == nxt \
                        and last_lane[k] != nxt
                    if gap >= TELEGRAPH and not (rev and gap < REV_MIN) \
                            and last_beat[k] < best_t:
                        cand, best_t = k, last_beat[k]
            if cand >= 0:
                j += 1; grp_start = e["beat"]
                e["lane"] = seq[j % 3]
                forced_pick = cand
                last_fast_sec = sec; prev_was_fast = True
            else:
                e["lane"] = last_ev_lane      # park in the same lane
                prev_was_fast = False
        else:
            if last_ev_lane is None: grp_start = e["beat"]
            elif e["beat"]-grp_start >= GRP_BEATS:
                j += 1; grp_start = e["beat"] # cascade steps to the next lane
            e["lane"] = seq[j % 3]
            prev_was_fast = False
        last_sec = sec; last_ev_lane = e["lane"]
        # density-adaptive tiers, in order of preference:
        #  A continue the cascade: airborne gap in [pref, max_air], no reversal
        #  B bring in a ball: fresh/expired thread (gap > max_air -> chain
        #    split -> a new pel enters) — sparse passages shed balls, dense
        #    passages summon them (organic)
        #  C same window but allow a SLOW reversal (>= REV_MIN beats)
        #  D compromise gap down to the hard floor, no fast reversal
        #  E last resort: oldest thread outright (check() flags if this ever
        #    creates a fast reversal or sub-floor airtime)
        # within a tier: strongly avoid the ball that ALREADY sits on this
        # lane (same ball twice in a row on one lane is dull — two DIFFERENT
        # balls landing the same lane back-to-back is great), then prefer the
        # thread whose previous airtime matches this gap (steady hop rhythm ->
        # repeating, predictable patterns), tie-broken oldest-first
        def tier(pred):
            best, best_key = -1, None
            for k in range(K):
                gap = e["beat"] - last_beat[k]
                if gap <= 0: continue   # a ball can't catch two simultaneous notes
                # a reversal is out-AND-back (a -> b -> a); same-lane bounces
                # are handled by the self-bounce penalty below instead
                rev = prev_lane[k] is not None and prev_lane[k] == e["lane"] \
                    and last_lane[k] != e["lane"]
                if not pred(gap, rev): continue
                self_bounce = 1 if last_lane[k] == e["lane"] else 0
                mism = round(2*abs(gap-air[k])) if air[k] is not None else 0
                key = (self_bounce, mism, last_beat[k])
                if best_key is None or key < best_key: best, best_key = k, key
            return best
        pick = forced_pick
        if pick is None:
            pick = tier(lambda g, r: MIN_GAP_PREF <= g <= max_air and not r)
            # steady rhythms alias with the 3-lane rotation: the same few
            # balls cycle period-3 and each lands its own old lane forever.
            # If the best airborne pick would self-bounce, wake an idle
            # thread instead — a fresh ball enters and the pattern rotates.
            if pick >= 0 and last_lane[pick] == e["lane"]:
                alt = tier(lambda g, r: g > max_air)
                if alt >= 0: pick = alt
            if pick < 0: pick = tier(lambda g, r: g > max_air)
            if pick < 0: pick = tier(lambda g, r: MIN_GAP_PREF <= g <= max_air and g >= REV_MIN)
            if pick < 0: pick = tier(lambda g, r: g >= MIN_GAP_HARD and not (r and g < REV_MIN))
            if pick < 0: pick = tier(lambda g, r: True)
        e["thread"] = pick
        prev_lane[pick] = last_lane[pick]; last_lane[pick] = e["lane"]
        gap = e["beat"] - last_beat[pick]
        air[pick] = gap if gap <= max_air else None   # chain split resets rhythm
        last_beat[pick] = e["beat"]
    # threads -> chains
    chains = []
    for k in range(K):
        idx = [i for i, e in enumerate(ev) if e["thread"] == k]
        cur = None; last = -1e9; lane_c = None
        for i in idx:
            gap_sec = tempo.sec(ev[i]["beat"])-tempo.sec(last)
            selfb = lane_c is not None and ev[i]["lane"] == lane_c \
                and gap_sec < SELF_SPLIT_SEC
            # a ball must land at least TWICE before leaving: a lone ball may
            # take one fast self-bounce, and may float up to hard_air_sec,
            # rather than exit after a single catch
            lone = cur is not None and len(cur["ev"]) == 1
            split = (cur is None
                     or (gap_sec > max_air_sec and not (lone and gap_sec <= hard_air_sec))
                     or (selfb and not lone)
                     or (chain_cap is not None and K > 3 and len(cur["ev"]) >= chain_cap))
            if split:
                cur = dict(thread=k, ev=[]); chains.append(cur)
            cur["ev"].append(i); ev[i]["ball"] = len(chains)-1
            last = ev[i]["beat"]; lane_c = ev[i]["lane"]
    return ev, chains


# ---------------- optimal ball assignment (min path cover) ----------------
# "Balls stay in play as much as possible" is exactly MINIMUM PATH COVER on
# the DAG of legal catch-successions: solved EXACTLY by maximum bipartite
# matching (Kuhn), with edge order expressing aesthetic preference, a 2-opt
# pass trading reversals away, and a rescue pass for singletons. The tier
# heuristic in choreograph() still assigns LANES (and its thread sim gates
# fast lane changes); its ball grouping is replaced by this optimum.

def _edge_ok(ev, times, fastch, i, j, hard_air_sec):
    gs = times[j]-times[i]
    if gs <= 0 or gs > hard_air_sec: return False
    if ev[j]["beat"]-ev[i]["beat"] < MIN_GAP_HARD: return False
    if ev[i]["lane"] == ev[j]["lane"] and gs < SELF_SPLIT_SEC: return False
    if fastch[j] and ev[j]["beat"]-ev[i]["beat"] < TELEGRAPH: return False
    return True

def optimize_chains(ev, tempo, max_air_sec, hard_air_sec, chain_cap=None):
    n = len(ev)
    times = [tempo.sec(e["beat"]) for e in ev]
    fastch = [False]*n
    for j in range(1, n):
        dt = times[j]-times[j-1]
        if 0 < dt < MIN_MOVE_SEC and ev[j]["lane"] != ev[j-1]["lane"]:
            fastch[j] = True
    edges = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i+1, n):
            if times[j]-times[i] > hard_air_sec: break
            if not _edge_ok(ev, times, fastch, i, j, hard_air_sec): continue
            gs = times[j]-times[i]
            cost = abs(gs-2.5) + (1.5 if ev[i]["lane"] == ev[j]["lane"] else 0) \
                + (4 if gs > max_air_sec else 0)
            edges[i].append((cost, j))
        edges[i].sort()
    # maximum matching = fewest balls (exact)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 4*n+1000))
    match_head = [-1]*n; match_tail = [-1]*n
    def aug(i, seen):
        for _, j in edges[i]:
            if seen[j]: continue
            seen[j] = True
            if match_head[j] < 0 or aug(match_head[j], seen):
                match_head[j] = i; match_tail[i] = j
                return True
        return False
    for i in range(n):
        if edges[i]: aug(i, [False]*n)
    chains = []
    for j in range(n):
        if match_head[j] < 0:
            c = [j]
            while match_tail[c[-1]] >= 0: c.append(match_tail[c[-1]])
            chains.append(dict(ev=c))
    # 2-opt: swap successors between balls to trade reversals away
    # (fast reversals weigh lexicographically above slow ones)
    def is_rev(p, a, b):
        return p is not None and ev[p]["lane"] == ev[b]["lane"] \
            and ev[a]["lane"] != ev[b]["lane"]
    def rev_flags(seq):
        fast = slow = 0
        for k in range(2, len(seq)):
            if is_rev(seq[k-2], seq[k-1], seq[k]):
                if ev[seq[k]]["beat"]-ev[seq[k-1]]["beat"] < REV_MIN: fast += 1
                else: slow += 1
        return (fast, slow)
    for _ in range(8):
        improved = False
        links = [(ci, k) for ci, c in enumerate(chains)
                 for k in range(1, len(c["ev"]))
                 if is_rev(c["ev"][k-2] if k >= 2 else None, c["ev"][k-1], c["ev"][k])]
        if not links: break
        for ci, k in links:
            A = chains[ci]["ev"]
            if k >= len(A): continue
            if not is_rev(A[k-2] if k >= 2 else None, A[k-1], A[k]): continue
            done = False
            for cj, c2 in enumerate(chains):
                if done or cj == ci: continue
                B = c2["ev"]
                for m in range(1, len(B)):
                    if not _edge_ok(ev, times, fastch, A[k-1], B[m], hard_air_sec): continue
                    if not _edge_ok(ev, times, fastch, B[m-1], A[k], hard_air_sec): continue
                    na, nb = A[:k]+B[m:], B[:m]+A[k:]
                    fa, sa = rev_flags(na); fb, sb = rev_flags(nb)
                    f0, s0 = rev_flags(A); f1, s1 = rev_flags(B)
                    if (fa+fb, sa+sb) < (f0+f1, s0+s1):
                        chains[ci]["ev"], chains[cj]["ev"] = na, nb
                        improved = True; done = True; break
        if not improved: break
    # singleton rescue: merge lone balls into a neighbor chain
    changed = True
    while changed:
        changed = False
        for c in list(chains):
            if len(c["ev"]) != 1: continue
            i = c["ev"][0]
            for c2 in chains:
                if c2 is c: continue
                h, t = c2["ev"][0], c2["ev"][-1]
                if 0 < times[h]-times[i] <= hard_air_sec \
                        and ev[h]["beat"]-ev[i]["beat"] >= MIN_GAP_HARD:
                    c2["ev"] = [i]+c2["ev"]; chains.remove(c); changed = True; break
                if 0 < times[i]-times[t] <= hard_air_sec \
                        and ev[i]["beat"]-ev[t]["beat"] >= MIN_GAP_HARD:
                    c2["ev"] = c2["ev"]+[i]; chains.remove(c); changed = True; break
    # safety: split any remaining fast reversal; cap chain length (stream)
    out = []
    for c in chains:
        cur = [c["ev"][0]]
        for k in range(1, len(c["ev"])):
            i = c["ev"][k]
            if (len(cur) >= 2 and is_rev(cur[-2], cur[-1], i)
                    and ev[i]["beat"]-ev[cur[-1]]["beat"] < REV_MIN) \
                    or (chain_cap is not None and len(cur) >= chain_cap):
                out.append(dict(ev=cur)); cur = [i]
            else:
                cur.append(i)
        out.append(dict(ev=cur))
    out.sort(key=lambda c: c["ev"][0])
    for ci, c in enumerate(out):
        c["thread"] = ci % 7
        for i in c["ev"]: ev[i]["ball"] = ci
    return out

def check(song, ev, chains, label, hard_air_sec=4.5):
    t = song["tempo"]; problems = []
    min_ev = song.get("min_event", MIN_EVENT_SEC)
    times = [t.sec(e["beat"]) for e in ev]
    first_of_chain = {c["ev"][0] for c in chains}
    prev_gap = {}  # event index -> airtime of the throw landing there (beats)
    for c in chains:
        for a, b in zip(c["ev"], c["ev"][1:]): prev_gap[b] = ev[b]["beat"]-ev[a]["beat"]
    prev_fast = False; fast_n = 0
    for i in range(1, len(ev)):
        dt = times[i]-times[i-1]
        if dt == 0 and ev[i]["lane"] == ev[i-1]["lane"]: continue   # chord pair
        if dt < min_ev: problems.append(f"event spacing {dt:.3f}s")
        fastch = dt < MIN_MOVE_SEC and ev[i]["lane"] != ev[i-1]["lane"]
        if fastch:
            fast_n += 1
            if prev_fast: problems.append(f"consecutive fast changes at beat {ev[i]['beat']}")
            telegraphed = i in first_of_chain or prev_gap.get(i, 0) >= TELEGRAPH
            if not telegraphed: problems.append(f"untelegraphed fast change at beat {ev[i]['beat']}")
        prev_fast = fastch
    same = rev = fast_rev = throws = 0
    for c in chains:
        for i in range(1, len(c["ev"])):
            a, b = ev[c["ev"][i-1]], ev[c["ev"][i]]
            throws += 1
            if a["lane"] == b["lane"]: same += 1
            if i >= 2 and ev[c["ev"][i-2]]["lane"] == b["lane"] and a["lane"] != b["lane"]:
                rev += 1
                if b["beat"]-a["beat"] < REV_MIN: fast_rev += 1
            gap = b["beat"]-a["beat"]
            gs = t.sec(b["beat"])-t.sec(a["beat"])
            if not (MIN_GAP_HARD <= gap and gs <= hard_air_sec):
                problems.append(f"airtime {gs:.2f}s at {b['beat']}")
    if fast_rev: problems.append(f"{fast_rev} FAST reversals")
    singles = sum(1 for c in chains if len(c["ev"]) < 2)
    print(f"  {label}: {len(chains)} chains ({singles} single-catch), {throws} throws, "
          f"same-lane {100*same/max(1,throws):.1f}%, reversals {100*rev/max(1,throws):.1f}%, "
          f"fast-rev {fast_rev}, {'OK' if not problems else 'PROBLEMS: '+'; '.join(problems[:5])}")
    return not problems

def emit(song):
    t = song["tempo"]
    modes = {}
    ok = True
    playable, ghosts = thin(song)
    for mode, m in MODES.items():
        ev, chains = choreograph(song, m["K"], playable,
                                 m["max_air_sec"], m["hard_air_sec"], m["chain_cap"])
        chains = optimize_chains(ev, song["tempo"], m["max_air_sec"],
                                 m["hard_air_sec"], m["chain_cap"])
        ok &= check(song, ev, chains, f"{song['name']}/{mode}", m["hard_air_sec"])
        events = [[round(t.sec(e["beat"]), 4), round(e["beat"], 3), e["lane"],
                   e["ball"], round(e["vel"], 2)] + e["midis"] for e in ev]
        cs = [dict(thread=c["thread"], ev=c["ev"],
                   entryT=round(t.sec(ev[c["ev"][0]]["beat"]) - 2*t.spb_at(ev[c["ev"][0]]["beat"]), 4),
                   exitT=round(t.sec(ev[c["ev"][-1]]["beat"]) + 2*t.spb_at(ev[c["ev"][-1]]["beat"]), 4))
              for c in chains]
        modes[mode] = dict(events=events, chains=cs)
    beats = [round(t.sec(b), 4) for b in range(int(song["beats_len"])+1)]
    return dict(name=song["name"], title=song["title"], spb0=round(t.spb_at(0), 4),
                maxAir=max(m["max_air_sec"] for m in MODES.values()),
                dur=round(t.sec(song["beats_len"]), 3), beats=beats,
                back=[[round(t.sec(b), 4), m, round(v, 2),
                       round(min(t.sec(b+d)-t.sec(b), 2.5), 3)]
                      for b, m, v, d in song["back"]],
                perc=[[round(t.sec(b), 4), k] for b, k in song["perc"]],
                ghosts=[[round(t.sec(g["beat"]), 4), round(g["vel"], 2)] + g["midis"]
                        for g in ghosts],
                modes=modes), ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true", help="write even if checks fail")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--mid"); ap.add_argument("--title")
    ap.add_argument("--melody-track", type=int)
    args = ap.parse_args()
    songs = [korobeiniki()]
    for p in sorted((HERE/"midi").glob("*.mid")):   # drop .mid files here to add songs
        songs.append(song_from_midi(p, SONG_CFG.get(p.stem, {}).get("title")
                                    or p.stem.replace("_", " ").title()))
    if args.mid:
        songs.append(song_from_midi(args.mid, args.title, args.melody_track))
    out, all_ok = [], True
    print("choreography check:")
    for s in songs:
        data, ok = emit(s); out.append(data); all_ok &= ok
    if args.write and not all_ok and not args.force:
        print("checks FAILED — refusing to write (use --force to override)")
        sys.exit(1)
    if args.write:
        js = "const SONGS=" + json.dumps(out, separators=(",", ":")) + ";"
        html = INDEX.read_text()
        a = html.index("/*SONGS_START*/"); b = html.index("/*SONGS_END*/")
        INDEX.write_text(html[:a+15] + "\n" + js + "\n" + html[b:])
        print(f"wrote {len(js)//1024} KB of song data into {INDEX.name}")
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()

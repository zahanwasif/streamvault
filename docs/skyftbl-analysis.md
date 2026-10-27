# How Skyftbl edits Shorts: 10 latest, measured

**Channel:** [@Skyftbl77](https://www.youtube.com/@Skyftbl77), 863K subscribers, IShowSpeed clips. These are the 10 newest Shorts as of 2026-10-28 (posted 16–27 Oct).

**How it was measured:** each Short was downloaded for analysis only (it wasn't added to the library or committed), then:
- **Cuts:** ffmpeg scene detection, with flicker bursts closer than 0.35 s merged into one.
- **Loudness:** EBU R128; momentary loudness median for the first 3 s, the middle, and the last 3 s.
- **Speech:** word timestamps from VoiceStudio.
- **Music bed:** the energy under 140 Hz against the voice band, split into speech and pause windows.
- **Sound effects:** off-beat transients, classified by spectral shape.
- **Frames:** 4 fps for the first 4 s, then 1 fps, read by eye.

**Music:** none of the 10 has a YouTube music credit or names a song, so no originals could be identified or downloaded. The music is described from analysis instead.

The prompt this produced is in [`skyftbl-prompt.txt`](skyftbl-prompt.txt) and is saved as the Style instructions.

## The numbers

| # | Title | Length | Views | Likes % | Cuts (every) | LUFS | First 3 s / middle / last 3 s | Music vs voice | Talking | Effects (first 3 s) |
|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Speed Sees Ruddy as His Father 🥹❤️ | 42 s | 2,184* | 6.5 | 12 (3.2 s) | -12.9 | -11.2 / -14.1 / -17.0 | -5.9 dB | 72% | 10 (2) |
| 02 | Speed Met the Coolest Lady Ever 😭🔥 | 28 s | 34,051 | 1.94 | 10 (2.5 s) | -11.8 | -14.9 / -12.7 / -16.6 | -22.9 dB | 98% | 4 (5) |
| 03 | Speed Tried to POP His Eye Out 💀😭 | 30 s | 28,580 | 1.09 | 17 (1.7 s) | -10.5 | -15.2 / -11.4 / -15.0 | -12.3 dB | 70% | 5 (5) |
| 04 | Why Does Speed Treat Slipz Like This?🥺💔 | 35 s | 29,967 | 1.72 | 11 (2.9 s) | -15.2 | -14.9 / -16.6 / -10.5 | -10.8 dB | 56% | 6 (5) |
| 05 | IShowSpeed Has a Soft Spot for Cats 🥹❤️ | 39 s | 28,009 | 2.21 | 12 (3.0 s) | -17.3 | -15.5 / -18.1 / -20.0 | -11.8 dB | 81% | 7 (2) |
| 06 | He THOUGHT it was a cute cat… then this happened 😭🤣 | 28 s | 5,829 | 1.49 | 5 (4.7 s) | -7.3 | -6.8 / -13.2 / -14.1 | -10.7 dB | 30% | 2 (1) |
| 07 | Speed Was NOT Expecting This 😭😂 | 35 s | 20,654 | 1.91 | 25 (1.4 s) | -9.0 | -7.3 / -10.2 / -16.4 | -10.6 dB | 86% | 4 (2) |
| 08 | Speed Is Every Dad's Dream Son 🫶 | 31 s | 42,833 | 2.33 | 10 (2.8 s) | -8.5 | -8.3 / -9.8 / -11.8 | -3.7 dB | 28% | 2 (2) |
| 09 | Speed Finally Reveals His Sisters! 😭❤️ | 34 s | 63,173 | 1.58 | 18 (1.8 s) | -11.7 | -10.6 / -14.6 / -14.9 | -8.5 dB | 75% | 10 (6) |
| 10 | Everyone Thinks Speed Hates Slipz… 💔 | 47 s | 175,086 | 1.78 | 17 (2.6 s) | -14.2 | -7.1 / -17.4 / -21.3 | -7.9 dB | 88% | 5 (7) |

\* 01 was posted the day of the analysis, so its views and like rate aren't comparable yet.

"Music vs voice" compares the bass band (mostly music) with the voice band while someone is talking: −10 dB means the music sits about 10 dB under the voice. "Effects" counts off-beat transients the music doesn't explain: whooshes, hits, twinkles, and some loud reactions.

**Medians:**
- 34 s long (28–47);
- a real cut every 2.7 s;
- −11.8 LUFS integrated, with the first 3 s 2.7 dB hotter than the middle and the last 3 s 2 dB softer;
- music about 10.6 dB under the voice, ducking 0.4 dB (effectively none);
- talking 74% of the time at 2.4 words per second;
- 5 effects, 3.5 of them in the first 3 s;
- like rate 1.8%.

## Video by video
**01: Speed Sees Ruddy as His Father** (wholesome, 42 s)
- **Story:** told in order with no cold open. The first word lands at 0.0 s with "I PAYED FOR YOUR TWO HOT DOGS".
- **Captions:** mid-frame, revealed word by word, each key word in a different colour (green, red, cyan, yellow).
- **Narration and speakers:** asterisk tags for time jumps and actions ("*FEW MINUTES LATER*", "*CONFUSED*", "*RUDY FIGURES IT OUT*") and speaker tags ("RUDY: HE IS A ROCKSTAR").
- **Look:** a warm, saturated grade with soft glow; push-ins on Speed.
- **Ending:** the emotional line "SOMETIMES YOU REMIND ME OF MY FATHER", then a ❤️ end card, "MORE THAN A BODYGUARD".
- **Music:** a melodic bed throughout, close under the voice (−6 dB).

**02: Speed Met the Coolest Lady Ever** (fan, 28 s)
- **Opening:** the fan's strongest line first, "I KNOW WHO YOU ARE", cut to Speed's "WHO AM I" at 1.5 s.
- **Icon pop:** a YouTube logo appears when she says "YouTube".
- **Music:** none under the conversation (98% talking, music −23 dB). A soft bed comes in for the last 3 s.
- **Ending:** a slowed, warm, vignetted hug with ❤️ and "COOL LADY".

**03: Speed Tried to POP His Eye Out** (funny, 30 s)
- **Cold open:** the only one of the ten. The ~3.8 s build and payoff come first, with a yellow flash between shots.
- **Hook banner:** a green typed-on line, "*SPEED MEETS EYE POPPER*". Five whooshes and flashes land in the first 2 s.
- **Story:** after the cold open it restarts from the beginning, with fast cuts (one every 1.7 s).
- **Captions:** coloured key words; "SH*T" is censored.
- **Ending:** 💀 at the end.
- **Music:** a bass-heavy beat about 12 dB under the voice.

**04: Why Does Speed Treat Slipz Like This?** (funny / sympathy, 35 s)
- **Character intro:** in the first second a yellow arrow and a "SLIPZ" label point him out, then "*SLIPZ LOOKING FOR FOOD*" types on.
- **Pointers:** green circles mark the food and the phone.
- **Close-ups:** tight face crops on the wrestling payoff (20–23 s).
- **Ending:** desaturated and vignetted, with 💔 and "*JUSTICE FOR SLIPZ*". The music changes to a new, fuller outro bed right at the end card, which is why the last 3 s are the loudest part.

**05: IShowSpeed Has a Soft Spot for Cats** (wholesome, 39 s)
- **Opening:** "OH IT'S A CAT", with a "CAT" arrow pointing at it at 1.2 s.
- **Mid-clip:** "*THE CAT RUNS AWAY*", then a slow push-in on Speed with a green arrow and "BRO GETS IGNORED". Music and an effect fill the 4 s where nobody talks.
- **Middle:** a hard cut to his stream setup with single colour words ("BRUH", pink "MEOW").
- **Ending:** ❤️ "SPEED LOVES CATS".
- **Level:** the quietest of the ten (−17.3 LUFS).

**06: He THOUGHT it was a cute cat…** (reaction, 28 s, weakest of the ten)
- **Layout:** stacked, with the streamer on top and the clip underneath. The chat fills the screen briefly as context ("*CHAT SAYING SQUINT YOUR EYES*").
- **Audio:** only 30% talking, so the music carries it. A meme effect at 18 s, then the scream.
- **Ending:** 😂 "CHAT DID HIM DIRTY".
- **Performance:** fewest cuts, longest title, lowest views. The slow one is the one that underperformed.

**07: Speed Was NOT Expecting This** (funny, 35 s)
- **Shake bursts:** 35 of its 60 detected scene changes are shake or glitch bursts on the hype lines, at 5–6 s and 22–26 s.
- **Transition:** a full-frame yellow colour flash at 3.0 s, matching his jersey.
- **Captions:** keywords coloured by meaning (blue "AUSTRALIAN", red "SHARKS").
- **Ending:** desaturated, with ☠️ and a red arrow, "BRO WAS NOT READY".
- **Music:** a steady melodic bed (−9.0 LUFS).

**08: Speed Is Every Dad's Dream Son** (wholesome, 31 s)
- **Layout:** stacked, Speed on top and his dad's video call underneath.
- **Money pop:** a 💵 stack with "400$" and then "4000$", with a green tint flash on the reveal.
- **Audio:** whooshes on the fast opening cuts; only 28% talking, so the music is up close (−3.7 dB vs voice).
- **Ending:** ❤️ "BEST DAD".

**09: Speed Finally Reveals His Sisters!** (wholesome, 34 s)
- **Opening:** a shouted "DAD" in a tight punch-in, the loudest first second of the ten.
- **Inserts:** a childhood photo with a yellow arrow and a "BRAID" label; "SISTER" labels with arrows; a decorative audio-visualiser graphic.
- **Twinkle effect:** a small high-pitched shimmer on each label pop-in. It also shows up in 03, 04 and 10.
- **Ending:** ❤️ "CUTE SISTERS".

**10: Everyone Thinks Speed Hates Slipz…** (loyalty, 47 s, the best performer at 175K)
- **Opening:** the premise said to camera, "IT IS SLIPZ BIRTHDAY CHAT". Then a teal flash, a "SLIPZ" arrow label and the twinkle.
- **Price tags:** "29,000$" and "62,170$" on the watches.
- **Build:** a long, talk-driven emotional build (88% talking) with a soft bed, and the loudest opening of the ten.
- **Ending:** a themed end card with confetti, a party hat, ❤️ and "SLIPZ DESERVES THIS". The last 3 s are the softest of the ten (−21 LUFS momentary).

## What repeats: the house style
1. **Story arc.** Every Short is premise → turn → payoff → verdict, and it ends on an emotional summary line. That's why they run 28–47 s.
2. **Opening.** The first word lands at 0.0 s in 6 of the 10. They open on the premise line or the loudest reaction, never an establishing shot. Only 1 of the 10 uses a cold open and a hook banner; the first caption is the hook.
3. **Front-loaded energy.** The opening is ~3 dB louder than the middle, and half the effects sit in the first 3 s.
4. **Captions.** Always mid-frame, in a heavy rounded font, white, revealed as spoken, 2–4 words. Key words get a colour by meaning, with a glow, and swearing is censored.
5. **Pacing.** A real cut every ~2.7 s, plus zooms, labels and flashes between cuts. Hype lines get shake bursts.
6. **Music.** Always there, as a steady bed about 10 dB under the voice, not ducked. It gets louder when nobody talks, and it changes or swells for the end card. The mood follows the story: warm and emotional for family and loyalty, bouncier for chaos.
7. **Sound effects.** Few and purposeful:
   - whooshes and flashes on the opening cuts;
   - a twinkle on every label;
   - a hit on a shock.
   They're never stacked on dialogue.
8. **Annotations.** Arrows and name labels introduce people; circles mark objects; asterisk tags narrate jumps; emoji and icons react to words (money, YouTube, skull, heart).
9. **Ending.** A themed end card: the verdict line, an emoji, desaturation and vignette, and the music changing or softening. They're not built to loop: the first and last frames differ (median histogram difference 0.64).
10. **Copy.**
    - **Titles:** "Speed + what happened", 30–45 characters, ending in two feeling emojis (😭❤️, 💀😭, 🥹❤️).
    - **Descriptions:** a 2–3 sentence retelling with emojis, sometimes a question to start comments, then 8–10 hashtags.
11. **What wins.** The wholesome and loyalty stories (10, 09, 08) got 2–5x the views of the funny ones.

## What StreamVault can reproduce today, and what it can't
**Can, and the prompt asks for it:**
- the story arc and the verdict ending (picks);
- opening on the premise line, and a cold open only for visual shocks;
- mid-frame revealed captions with one meaning-picked highlight colour;
- punch-in zooms front-loaded, following the face, and the stacked layout for reactions and calls;
- a steady un-ducked music bed about 10 dB under the voice, with the mood by story;
- sparse effects with whooshes on the opening cuts;
- titles and descriptions in their format.

**Can't yet (possible follow-ups):**
- **Several highlight colours in one caption:** they colour each key word by meaning. Our renderer has one highlight colour per clip.
- **Narration and speaker tags mid-clip** ("*FEW MINUTES LATER*", "RUDY:").
- **Arrows, name labels and circles** with a twinkle effect.
- **Emoji and icon pops** tied to words (💵, YouTube logo, 💀, ❤️).
- **A themed end card:** emoji, desaturation and vignette, with the music changing.
- **Colour-flash transitions and shake bursts.** We only have a white flash out of a cold open.
- **Censoring swear words** in captions ("SH*T").
- **Their 1080x1400 frame.** We render a full 9:16 1080x1920.

**Two settings this analysis would change; they're yours to set:**
- Their typical length is 30–47 s, so **Longest 50 s** would fit the best performer (47 s); yours is 45.
- The prompt asks for no loop ending, a steady un-ducked music bed at 0.45, and no hook banner unless needed. A test render at 0.3 measured the music 14.4 dB under the voice against their 10.6, so the prompt uses 0.45. Claude applies those per clip from the instructions.

## Appendix: the instructions this replaced
```
Audience: strangers scrolling Shorts who have never seen this streamer. Every clip must work with zero context.

Picks: only moments with a clear payoff inside the clip (a big reaction, a punchline, chaos, a sweet moment). Skip sponsor reads, waiting around, menus, loading screens, and anything that needs earlier context. Zero Shorts from a weak video is fine.

Hook: open with a 2-3 s cold open of the payoff (the reaction face, not the setup). Hook text 4-7 words, ALL CAPS, curiosity not spoilers, e.g. "SHE SWORE SHE KNEW HIM". Neon green, typed on, middle of the screen. Use the voice hook (narrator voice) only when the cold open has little talking of its own.

Captions: white heavy font, 2 words at a time, words appear as they're spoken, punch word highlighted in neon green; on hype clips highlight in yellow instead. Keep them off the face.

Pacing: something changes every 2-3 seconds. Punch-in zoom on every reaction and punchline (subtle, about 1.12; bigger, about 1.25, on the single biggest moment). Jump-cut any pause longer than 0.7 s. Keep the main person's face in frame and follow them when they move.

Sound: bass boom on the punchline, crowd laugh on the funniest beat, gasp or "ooh" on shocking moments, record scratch on awkward stops, ding when someone gets something right. At most 3 effects per clip besides the whooshes on cuts. Always pick a music mood (phonk/hype for chaos, bouncy for funny, lo-fi for sweet) so it applies once the music library has tracks.

Ending: end on a reaction or line that flows back into the cold open so the replay feels seamless. No outro, no "follow for more".

Titles: creator's name first, curiosity-driven, under 60 characters, no ALL CAPS, no lies.
```

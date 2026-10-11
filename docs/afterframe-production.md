# THE AFTERFRAME

Tagline: Every Frame Hides a Story.
Audience: English-speaking US viewers.
Launch plan: one reviewed short per day for 30 days.

## Applied defaults

- Pexels stock footage, portrait 9:16 (1080 x 1920).
- Sequential concatenation, no transitions, center-crop fill.
- Source clips up to 3 seconds at 1.00x; one output per run.
- US English Guy male Edge TTS at 0.95x, narration volume 1.0.
- Local random background music at 0.12 volume. Review the selected track;
  random selection does not guarantee a cinematic score or commercial rights.
- English sentence captions, white bold text with black outline, no animation.
- Narration-only brand prompt targets approximately 75-85 words and 35-40 seconds.
- Engine exports at 30 FPS with AAC audio. Standard default encoder is H.264;
  check the actual exported file if selecting a different encoder.
- Upload targets are YouTube and Instagram; automatic upload is OFF.

Settings live in config.toml. Keep this file private because it contains credentials.
The app setting final_video_export_dir optionally saves an additional final MP4
copy under <export folder>/<task ID>/final-<index>.mp4. Internal task files stay
in storage for playback and history. Export failure leaves the original intact
and produces a task warning. Existing videos are not moved. On this Windows
installation, the export folder is MoneyPrint Ai Video on the OneDrive Desktop;
allow enough local disk space and OneDrive quota for the additional copies.
Script duration is a target, not an enforced runtime. Full Preview gives actual
voice duration; revise the script before rendering if it is outside 35-40 seconds.
Edge TTS provides a consistent voice but does not guarantee dramatic acting.

## Daily workflow

1. Select an original sci-fi mystery, emotional drama, or psychological suspense idea.
2. Generate narration, then inspect its hook, logic, escalation, and twist.
3. Use Full Preview to check natural delivery and actual duration.
4. Plan ordered shots and choose footage that depicts each story beat. Sequential
   mode preserves clip order; it cannot guarantee semantic matching or continuity.
5. For custom characters, create shots externally with a compatible AI video tool
   and reuse character references, then upload the ordered local clips. Pexels
   cannot create original scenes or preserve the same face across unrelated clips.
6. Generate locally, review framing, captions, audio mix, ending, and rights.
7. Review metadata and AI disclosure, then manually publish or use the platform's
   scheduler for 05:00 Asia/Kolkata. No daily background scheduler is configured.
8. Track retention, viewed versus swiped away, shares, saves, and returning viewers.

Avoid an end-logo animation that interrupts the loop. Match sound effects manually
where needed; this preset does not add shot-specific effects automatically.
Keep social accounts connected in Upload-Post, but do not enable automatic public
posting until a complete reviewed generation and upload test succeeds.

## Separate production-pack prompt

Use this in a separate writing session, not the app's narration field:

You are the head writer for THE AFTERFRAME. Create one original 35-40 second
vertical cinematic story for US English viewers about [SUBJECT]. Use a strong
0-3 second hook, mystery at 3-15 seconds, escalation at 15-30 seconds, and a
twist/payoff by 40 seconds. Narrator: calm, warm American male at 0.95x.
Keep narration approximately 75-85 words and verify actual timing with audio.
Define each main character's age, face, hair, clothes, body type, and emotional
state, and preserve these within every shot. Use realistic motion, controlled
camera movement, cinematic lighting, and consistent grading. Do not imitate
existing films, characters, or creators. Do not add text inside generated visuals.
Return: title, premise, narration alone, timestamped storyboard, per-shot visual
prompts, camera/lighting, music/SFX direction, provisional subtitle timing,
YouTube title/description/hashtags, and Instagram caption. Subtitle timestamps
must ultimately be regenerated or corrected using the actual audio.

## Publish gate

- Strong opening, coherent original story, memorable ending.
- Character continuity and relevant visuals checked by a person.
- Narration clear; music below voice; captions synchronized and readable.
- Actual export verified: 1080 x 1920, 30 FPS, MP4/H.264/AAC, 35-40 seconds.
- Music, stock footage, and AI tools licensed for intended commercial use.
- Realistic synthetic media disclosed according to each platform's rules.
- No third-party watermark; title, caption, hashtags, and account targets reviewed.

## Growth plan

Days 1-30: test 3-4 story categories with consistent voice and one quality short daily.
Days 31-90: develop the strongest themes and test sequels/recurring characters.
Months 3-6: consider 2-5 minute stories when quality and audience demand support it.
Months 6-12: explore longer films, licensing, relevant sponsorships, and collaborations.
Use 05:00 IST as an initial timing experiment, not a guaranteed best posting time.
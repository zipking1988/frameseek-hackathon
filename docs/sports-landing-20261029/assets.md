# Sports landing assets

The user selected the first displayed concept on 2026-10-29. The visual truth is
`selected-option-1.png`. All new basketball photographs were generated with the
built-in ImageGen tool. No uploaded game footage or private investigation frames
were sent to ImageGen. These images are promotional illustrations, not source
evidence or model-recognition results.

| Asset in `src/frameseek/static/landing/` | Dimensions | Consuming surface |
| --- | --- | --- |
| `courtside-hero.jpg` | 1536 × 1024 | Full-width hero background |
| `shot.jpg` | 1672 × 941 | Source preview, shot frame and thumbnail |
| `court.jpg` | 1280 × 720 | Wide court example and thumbnails |
| `pass.jpg` | 1672 × 941 | Pass example and thumbnails |

The sample timestamps are explicitly fictional. The four-frame interaction reuses
the court illustration for two examples; it is a UI demonstration rather than a
continuous video. JPEG compression keeps the combined images to about 1.2 MiB.
The tool's original generations remain under the local Codex generated-images
directory; a working hero original is archived in ignored `.frameseek/` storage.

## Prompt set / art direction

- Hero: recreate the selected courtside composition as a 1536 × 1024 photographic
  background only. Anonymous adult shooter in plain charcoal kit at the right,
  cream-uniform defender in the foreground, ball overhead and orange rim at right.
  Dark indoor gym, warm hardwood, naturally even navy negative space on the left
  for HTML typography. No text, UI, logos, player identities or tracking overlays.
- Shot: individual 16:9 photograph matching the same scene and adult anonymous
  players. Contested jump shot, indoor steel rafters, warm hardwood and blurred
  spectators. No text, UI, named athletes, branding or evidence annotations.
- Court: individual 16:9 elevated sideline photograph of the same style of amateur
  game. Charcoal and cream uniforms, defender stepping toward the paint, ballhandler
  on the wing, readable court and hoop. No text, identities, logos or overlays.
- Pass: individual 16:9 photograph in the same gym, a player passing toward a
  teammate on the right wing. Match the selected lighting, kit and palette. No
  text, UI, identities, branding or tracking marks.

Typography uses the existing local Instrument Sans font; icons use existing
official Phosphor SVG assets and their existing license. Canvas 2D draws only
the sample questions' actual DOM-defined relationships to their evidence cards.

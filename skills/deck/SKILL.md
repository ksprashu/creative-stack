---
name: deck
description: Build brand-themed slide decks from a plain outline and export them to HTML, PDF, editable PPTX or per-slide PNGs, with the theme generated from brand tokens. Trigger via /deck.
disable-model-invocation: true
---

# Deck: Outline → Branded Slides

Talk structure is the work. Front matter, slide breaks, colour rotation and export plumbing
are not — this does those.

---

## 🎯 Goal

A flat markdown outline becomes a deck that already looks like it belongs to the brand: title
slide, chapter dividers on rotating accents, content slides that split before they overflow,
and a theme compiled from `brand/tokens.json` rather than hand-tuned CSS.

The intermediate is a normal Marp markdown file. It stays editable — everything downstream reads
that file, so a slide you dislike is a text edit, not a re-run of the generator.

---

## 📋 Step-by-Step Workflow

1. **Write the outline** as plain markdown. The shape carries the meaning:

   ```markdown
   # Deploying to Cloud Run          <- title slide
   > Ship a container in 90 seconds  <- subtitle

   ## Why it matters                 <- heading with no bullets = chapter divider
   ## Scales to zero                 <- heading with bullets = content slide
   - No idle cost
   - No YAML to write

   ```bash                           <- fenced code stays with its slide
   gcloud run deploy --source .
   ```
   ```

2. **Generate the deck.** Slides over `--max-bullets` split, keeping the heading with `(cont.)`:

   ```bash
   python3 scripts/outline.py -i talk-outline.md -o out/deck/talk.md \
     --footer "Google Cloud · @you" --outro "Thanks — questions?"
   ```

3. **Read `out/deck/talk.md` and fix it there.** This is the step people skip. The generator
   decides where slides break; you decide whether the break lands in the right place.

4. **Build.** The output extension picks the format, and the theme rebuilds itself whenever
   `brand/tokens.json` is newer than the generated CSS:

   ```bash
   python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.pdf
   python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.html
   python3 scripts/build.py -i out/deck/talk.md -o out/deck/png/   # one PNG per slide
   ```

5. **When someone needs PowerPoint they can edit**, pass `--editable` — without it the PPTX is
   one full-bleed image per slide and nobody can fix your typo:

   ```bash
   python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.pptx --editable
   ```

6. **Iterate live** with `--watch`, or inspect the theme alone:

   ```bash
   python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.html --watch
   python3 scripts/theme.py --profile google --stdout | less
   ```

---

## 💡 Concrete Example

A conference talk, outline to submitted PDF plus social cards:

```bash
# 1. outline -> deck, dark theme for a big room
python3 scripts/outline.py -i notes/cloudrun-talk.md -o out/deck/talk.md \
  --title "Deploying to Cloud Run" --subtitle "Ship a container in 90 seconds" \
  --footer "Google Cloud · KubeCon 2026" --dark --max-bullets 5 \
  --outro "Slides + code: goo.gle/cloudrun-talk"

# 2. read out/deck/talk.md, move two slide breaks, tighten three bullets

# 3. the artefacts the organisers and the blog both want
python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.pdf
python3 scripts/build.py -i out/deck/talk.md -o out/deck/talk.pptx --editable
python3 scripts/build.py -i out/deck/talk.md -o out/deck/png/

# 4. slide 1 becomes the social card, framed and backdropped
python3 ../shot/scripts/frame.py -i out/deck/png/talk.001.png \
  -o out/social/card.png --backdrop hero --chrome none
```

Speaker notes travel as HTML comments in the deck (`<!-- note: … -->`) and come out in the
PDF with `--notes`.

---

## 🚫 Hard Constraints

*   **NEVER** hand-edit `templates/deck/.generated/*.css`. It is regenerated from
    `brand/tokens.json` on the next build and your change disappears. Change the tokens, or
    change `scripts/theme.py`'s template.
*   **NEVER** ship a `.pptx` without `--editable` to anyone who might need to change a word.
    Marp's default PPTX is a deck of screenshots.
*   **NEVER** commit a deck that embeds a screenshot straight off the machine. Route it through
    `/shot` first — that is what redacts tokens, project IDs and account names.
*   **NEVER** assume a heading with bullets under it is a divider. `outline.py` decides purely on
    whether a body follows, so an empty section you meant as content silently becomes a divider.
*   **NEVER** raise `--max-bullets` past about 6 to avoid a split. If a slide genuinely has nine
    points, it is two slides, and the split is the generator telling you so.
*   **NEVER** point `-o` at a bare filename with no extension. The format comes from the suffix;
    a directory means per-slide PNGs, and anything else is rejected rather than guessed.

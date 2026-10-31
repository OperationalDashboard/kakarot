# Design skills Kakarot learned from

Kakarot's design rules (`design.py`) are distilled from these open-source skills. The original files are kept
here, unchanged, with their licences, so Kakarot (and you) can read the full versions.

| Skill | Author | Licence | Source (commit) |
| --- | --- | --- | --- |
| Taste Skill - art direction from a study of 247 real websites, house rules, real photos | Leonxlnx | MIT (`taste-skill.LICENSE`) | github.com/Leonxlnx/taste-skill (12af471) |
| Impeccable - design vocabulary, craft floor, AI-look tells | Paul Bakaus | Apache 2.0 (`impeccable.LICENSE`) | github.com/pbakaus/impeccable (b7a8917) |
| Design engineering - motion, easing, timing, small details | Emil Kowalski | MIT (`emil-skills.LICENSE`) | github.com/emilkowalski/skill (a81dbca) |

The two tools from the same video:
- **Playwright** (browser testing): Kakarot already opens every site in a real browser at desktop and phone size,
  and now also measures the design rules there (`design.TASTE_JS`).
- **Figma**: with your Figma token saved (Websites → Publishing accounts), send Kakarot a Figma frame link and it
  builds the site from the design (`build_website_from_figma`).

Photos for new sites come from Openverse (openverse.org) - only licences that allow commercial use; credits are
saved next to the photos (`images/photos/CREDITS.json`) and shown in the site's footer.

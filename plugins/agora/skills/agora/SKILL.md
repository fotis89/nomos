---
name: agora
description: Agora - product research. Research which product to buy (appliances, laptops, electronics) for a Greek buyer, using the app's side-panel browser to read Skroutz, Reddit, YouTube transcripts, independent tests and spare-part sites, then deliver a weighted comparison as a self-contained HTML report. Use when the user says "which X should I buy", "product research", "recommend a laptop", "CAD laptop", "compare these models", "βρες μου καφετιέρα", "ποιο να αγοράσω", "σύγκρινε", "έρευνα αγοράς", "skroutz", or asks for a purchase recommendation.
argument-hint: "<product> [budget] [usage]"
---

# Product research

Method proven on two purchases (an espresso machine and a CAD laptop for a student). Goal: one defensible
pick at a real Greek price, backed by sources the user can open.

## 1. Clarify needs first

- Use `ask_user`, **one question at a time, with choices** (put the recommended one first). Typical order:
  1. Usage (drinks / software / workload).
  2. How hands-on the user wants to be (manual vs automatic, tinkering vs plug-and-play).
  3. Budget (ranges in €).
  4. Hard must-haves (size, noise, weight, ports, milk system…).
- Re-check needs whenever the user reveals new usage details, and say if it changes the pick
  (e.g. "I drink double cappuccino or lungo" changed the espresso pick).

## 2. Browse with the side-panel browser

Use the `browser` canvas, not `web_fetch`, for sites that block bots (Skroutz, Reddit, YouTube, test.de,
retailers, spare-part sites). The user can watch it.

- `open_canvas` with `canvasId: "browser"` and **one reused** `instanceId` (e.g. `research`).
- Drive it with `navigate_page` + `evaluate_javascript` (check `list_canvas_capabilities` for exact action names).
- Keep scripts short and return compact text (slice long strings). Long in-page waits time out: navigate,
  then re-run the extraction without the wait.

### Site recipes

- **Skroutz** (prices = the user's reference prices):
  - Search `https://www.skroutz.gr/search?keyphrase=<q>` or a category URL with `price_min`, `price_max`, `order_by`.
  - Product cards are `li.card`; `innerText` gives name | rating | reviews | price.
  - Product page: user reviews start at the heading `Αξιολογήσεις`.
- **Web search** when the search tool times out: `https://duckduckgo.com/html/?q=<q>`, read
  `.result a.result__a` and decode the `uddg` query param for the real URL. Bing in the panel was unreliable.
- **Reddit**: `https://www.reddit.com/search/?q=<q>`, then open threads; read `shreddit-post` and
  `shreddit-comment` (author, score, `[slot=comment]` text). old.reddit needs login; `.json` endpoints are blocked.
- **YouTube transcripts**:
  1. Open the video, click `#description-inline-expander #expand`, then the "Show transcript" button.
  2. Wait ~5 s, read `ytd-transcript-segment-renderer` (`.segment-timestamp`, `.segment-text`); dedupe repeats.
  3. 0 segments → reload and retry once; still 0 → skip and say so.
  4. Use the description's chapter timestamps to read only relevant sections.
- **Manufacturer pages**: specs and drink/feature lists often sit in hidden tabs; search
  `document.documentElement.textContent` for keywords.
- **Independent tests**: test.de (Stiftung Warentest) shows specs but grades are paywalled; testberichte.de
  summarises grades. Also IMTEST, Coffeeness, Which?, BBC Good Food (appliances), Notebookcheck (laptops).
- **Spare parts / running costs**: FixPart model pages (fetch `?category=...` in-page and parse);
  consumables (filters, descaler, beans, chargers) on Skroutz.

## 3. Breadth, then depth

1. **Shortlist ~10** from Skroutz filtered by the must-haves and budget; note rating, review count, price.
2. **Finalists (2–3)**: for each, gather
   - official specs,
   - several independent reviews/tests,
   - Reddit owner threads (long-term reliability, common faults),
   - YouTube reviewer transcripts (measured data, not opinions only),
   - spare-part prices and running costs.
3. **Verify claims that drive the decision** against primary evidence (e.g. a "max 8 g dose" claim was checked
   against measured transcript data and part names).
4. If new evidence contradicts something you said earlier, **correct it explicitly** in chat and in the report.

## 4. Weighted criteria

- Agree the criteria with the user: a **must-have filter** plus **weighted scores** (weights sum to 100).
- Score each finalist per criterion with the evidence behind the score.
- State which criteria were **deliberately ignored** and why (e.g. looks, brand loyalty).

## 5. Deliverable

A self-contained HTML report in the session `files/` folder (inline CSS, no external assets), in **Greek if the
user writes Greek**, then open it in the side-panel browser (`file:///…`). Sections, in order:

1. Verdict card: pick, Skroutz price, one-line why, runner-up.
2. Criteria table (must-haves, weights, ignored criteria).
3. Comparison table of finalists, each cell with its source.
4. Per-criterion evidence with short quotes and links/timestamps.
5. User and test ratings table (Skroutz, Amazon, test sites).
6. Common complaints and praises (owners, Reddit).
7. "Watch the model code" pitfalls: similar SKUs with different features/bundles.
8. Tips (setup, accessories, maintenance, where to buy).
9. Full source list.
10. Limitations: what was blocked, paywalled, unverified or estimated.

Chat reply stays short: **pick and price first**, 2–4 bullets, each claim with where it came from, unverified
items flagged. Point to the report for the rest.

## 6. Honesty rules

- Never present search-summary or AI-summary numbers as verified; open the source or mark it unverified.
- Mark estimates as estimates (running costs, lifespan).
- Note when several "sources" are the same reviewer or syndicated copies.
- When asked "where did you get this", point to the exact source, quote or video timestamp.
- Prices change: state the date checked.

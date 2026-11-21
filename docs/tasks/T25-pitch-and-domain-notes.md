# T25 Domain notes for prompts, slides, video (no code)

Owner: P3 · Depends on: – · Runs through the day

## Part A (hours 1–3): domain notes for the agent prompts
Write one short note (150–300 words) per agent: Sentinel, Detective, Skeptic, Growth, Marketplace, Discovery & Experience, SellerOS, Reporter. Save as `docs/domain-notes/<agent>.md`.
Each note answers:
1. What does an expert in this area check first, second, third?
2. Which innocent explanations are common (seasonality, campaigns, data delays)?
3. Which words and numbers must a good answer contain?
4. One example of a good answer in 3 sentences.
Use `docs/specs/00-overview.md` §2 (research) and `04-scenarios.md`. Developers paste these notes into the prompt files.

Also write the plain-English `description` of every metric in `docs/specs/05-metric-registry.md` §4 into `docs/domain-notes/metric-descriptions.md` (one line each). Dev 1 copies them into `config/metrics.yaml`.

## Part B (hours 15–20): slides
8 slides from `docs/specs/15-demo-and-pitch.md` §3. Charts from `uv run eye-sim export` files (ask Dev 1 to run it). One message per slide. Big numbers. No more than 25 words of text per slide.

## Part C (hours 20–22): rehearsal and video
1. Run the 3-minute demo script (`docs/specs/15-demo-and-pitch.md` §1, S8) 3 times with a timer. Note every place where the story is unclear and fix the words, not the system. Replace the [X]/[Y] placeholders with the numbers from the best run.
2. Record the backup video on the third run. Upload it; keep a local copy on two laptops.

## Done when
- `docs/domain-notes/` has 9 files.
- Slides exported as PDF in `docs/assets/pitch.pdf`.
- Video link in `docs/assets/README.md`.

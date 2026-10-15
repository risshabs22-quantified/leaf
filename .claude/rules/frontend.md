---
paths:
  - "frontend/**"
---
# Frontend rules
- No framework, no build step: `index.html`, `style.css`, `app.js`, served by FastAPI at `/` and `/static`.
- Every model-generated string is inserted through `esc()` or `textContent`. Never put raw model output in `innerHTML`.
- Mobile first: check layouts at 375px wide. The photo input has no `capture` attribute, so phones offer both camera and library.
- Keep the "How Leaf thought" panel: judges use it to see which NVIDIA model did each step.
- Colours come from the CSS variables in `:root`, with a dark-mode block. Labels on every input.
- Plain, kind wording for gardeners. Uncertainty is shown, never hidden.
- Static files are linked as `/static/<file>?v=<stamp>`. When you change a CSS or JS file, bump the stamp in every HTML page so returning visitors never mix old and new files.

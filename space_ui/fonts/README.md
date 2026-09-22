# Locally served theme fonts

Fonts are bundled so the interface works offline without sending font requests
to a third party. The default Grove theme retains its existing Inter files.

Neon uses the typography inspected on the official
[home page](https://www.quirq.ai/) and [products page](https://www.quirq.ai/products)
on 2026-09-22: Inter for body/headings, Poppins 600 for the brand wordmark,
and JetBrains Mono for technical labels and code. Its custom palette pairs warm
charcoal surfaces with soft magenta, violet and amber accents.

## Font provenance

- `inter/`: existing Inter 4.001 variable font; see `inter/LICENSE.txt`.
- `jetbrains-mono/JetBrainsMono-Latin.woff2`: the Latin variable font served by
  [quirq.ai](https://www.quirq.ai/_next/static/media/051742360c26797e-s.p.1bkzbscqrt8rl.woff2).
  Licensed under SIL OFL 1.1; `jetbrains-mono/LICENSE.txt` is copied from the
  [upstream project](https://github.com/JetBrains/JetBrainsMono/blob/master/OFL.txt).
- `poppins/Poppins-SemiBold-Latin.woff2`: Poppins 600 Latin served by
  [quirq.ai](https://www.quirq.ai/_next/static/media/e2334d715941921e-s.p.3o_v2fun1jzxk.woff2).
  Licensed under SIL OFL 1.1; `poppins/LICENSE.txt` is copied from
  [Google Fonts](https://github.com/google/fonts/blob/main/ofl/poppins/OFL.txt).

Quirq source stylesheets:
[global and typography](https://www.quirq.ai/_next/static/chunks/43k_pq_v4dqlj.css),
[page styling](https://www.quirq.ai/_next/static/chunks/37e7otw6-17l6.css),
[shared components](https://www.quirq.ai/_next/static/chunks/0l2arqgkmup02.css).

Noto Sans font files (Latin, Devanagari, Tamil, Telugu, Kannada, Malayalam --
Regular + Bold), bundled directly in the repo rather than depended on as a
system package, so headline-text compositing (see app/engine/text_overlay.py)
works identically in dev, CI, and on Render regardless of what's installed
at the OS level.

Licensed under the SIL Open Font License 1.1 (see OFL-LICENSE.txt) --
Copyright 2010-2020 Google Inc./Google LLC. Source: https://github.com/googlei18n/noto-fonts

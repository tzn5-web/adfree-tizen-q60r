# Q60R TizenBrew module

This repository now exposes a TizenBrew mods module in addition to the standalone WGT build.

The module uses the physically verified TizenTube 1.15.0 userscript as its patch engine and forces:
- AdBlock: enabled
- SponsorBlock: enabled
- SponsorBlock toasts: disabled

The module does not replace or uninstall Samsung's official YouTube application.

TizenBrew loads package.json from this repository through jsDelivr and injects tizenbrew/main.js into the YouTube TV page. The loader then fetches the pinned TizenTube 1.15.0 userscript and evaluates it.

Hardware validation status:
- Samsung QE43Q60RATXXH / Tizen 5.0
- TizenBrew: confirmed functional
- TizenTube 1.15.0 via TizenBrew: confirmed video playback with no ads
- Q60R custom module: not yet hardware-tested

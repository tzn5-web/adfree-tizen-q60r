# Q60R installation test

1. Enable Developer Mode/Homebrew using the standard Tizen Homebrew procedure for the TV.
2. Install `adfree-tizen-q60r-*-tizen5.0.wgt` through the Homebrew signing/install route.
3. Keep the stock Samsung YouTube application installed for rollback during the first test.
4. Launch `YouTube AdFree Q60R`.
5. Record whether the page loads, whether playback starts, and whether the remote remains responsive.
6. Test one signed-in session and one signed-out session.
7. Only after successful playback testing should default-feature behavior be assessed.

The generated widget is unsigned by design. Do not try to sideload it through a generic unsigned SDB path on a production TV; use the Tizen Homebrew signing route appropriate to the TV firmware.

`tables.py` and `pages/dashboard.jhtml` are copied unchanged from a real pinned dashboard
(the `ads` bot's Ad dashboard in boundcorp/ergo-bots, commit 6e9762c). `pages/dashboard_assets.jhtml` is
that page with four lines on top that load folder files (a style sheet that refers to an image, a
module script that imports another module, a plain script), because no pinned dashboard loads its
own assets yet; the asset files are small stand-ins.

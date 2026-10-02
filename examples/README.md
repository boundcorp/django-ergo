# Examples

- `*.py`: small scripts showing Ergo's knowledge base, RAG and feedback APIs.
- `hello/`: the smallest bot folder: `bot.yaml`, `agents.md` and one tool
  module. Run it with `docker run -v "$PWD/examples/hello:/bot" -p 8000:8000
  ghcr.io/boundcorp/ergonaut`.
- `proprietary/`: private example bots, such as the kitchen bot. This folder
  is git-ignored, so its bots never reach the public repo.

Run a bot folder with Ergonaut (see `../ergonaut/README.md`).

# Linux development setup

Zasz is developed mostly on Linux and played with on Windows. On Linux, StarCraft II runs as Blizzard's
free **headless** build: no window, no graphics and much lower resource use. That makes benchmarks
faster and lets several games run at once. It's also the same game version AI Arena runs matches on
(4.10), so local results are closer to ladder conditions.

These steps also work in WSL2 (e.g. Ubuntu on your Windows machine), with one extra setting. See
[Using WSL2](#using-wsl2).

## 1. Install the tools

```sh
sudo apt install git unzip wget
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, the Python project manager
```

## 2. Install StarCraft II (headless Linux build)

The bot is only a client that talks to a running SC2 process, so the game has to be on the same
machine. On Linux this is Blizzard's standalone package, **not** Battle.net: no account, launcher or
purchase.

Download **4.10** (the newest Linux build, and the one AI Arena uses) and unzip it to `~/StarCraftII`,
which is where python-sc2 looks by default. It's about a 4 GB download.

```sh
cd ~
wget https://blzdistsc2-a.akamaihd.net/Linux/SC2.4.10.zip
unzip -P iagreetotheeula SC2.4.10.zip    # creates ~/StarCraftII
rm SC2.4.10.zip
```

Typing the password `iagreetotheeula` means you accept the
[AI and Machine Learning License](http://blzdistsc2-a.akamaihd.net/AI_AND_MACHINE_LEARNING_LICENSE.html).
The full list of Linux packages is in the
[s2client-proto README](https://github.com/Blizzard/s2client-proto#linux-packages).

**Installed somewhere else?** Set `SC2PATH`, e.g. `export SC2PATH=/opt/StarCraftII` (add it to
`~/.bashrc`).

## 3. Add the maps

Put the ladder `.SC2Map` files directly in `~/StarCraftII/Maps/`. The Linux game looks for maps in
lowercase `maps/` and paths are case-sensitive, so also add a symlink, or games fail with
`InvalidMapPath`:

```sh
ln -s Maps ~/StarCraftII/maps
```

Then get the maps: either download the current pack from
https://aiarena.net/wiki/maps/ or copy them from the Windows install
(`F:\Games\Blizzard\StarCraft II\Maps\`), e.g. over the network:

```sh
mkdir -p ~/StarCraftII/Maps
scp "user@windows-pc:/F:/Games/Blizzard/StarCraft II/Maps/*.SC2Map" ~/StarCraftII/Maps/
```

Map files work on both systems, so the same ones are used everywhere.

## 4. Get the bot

```sh
git clone --recursive <repo-url> Zasz   # --recursive pulls the ares-sc2 submodule
cd Zasz
uv sync                                 # installs Python 3.12 if needed, then all dependencies
```

Already cloned without `--recursive`? Run `git submodule update --init`.

## 5. Check it works

```sh
uv run python run.py --difficulty VeryEasy
```

Nothing opens on screen (it's headless); the game runs in the background and the log ends with
`Result for player 1 - Zasz: Victory` (or `Defeat`). The replay goes to `replays/last_game.SC2Replay`.

Then run a benchmark:

```sh
uv run python scripts/benchmark.py --games 6 --difficulty Medium
```

## Notes

- **Replays:** games recorded on Linux use version 4.10. To watch one on Windows, open it in the
  normal game client, which downloads that old version the first time.
- **Resources:** each game uses roughly 1 CPU core and 1–1.5 GB of RAM, which sets how many can run
  in parallel with `scripts/benchmark.py --jobs N`.
- **Alternative:** AI Arena's Docker image `aiarena/arenaclient-sc2-base` has SC2 preinstalled. It's
  more setup, but useful later for bot-vs-bot matches with AI Arena's match runner
  ([sc2-ai-match-controller](https://github.com/aiarena/sc2-ai-match-controller)).

## Using WSL2

Inside WSL, python-sc2 detects WSL and by default launches the **Windows** StarCraft II instead of the
Linux build. To use the headless Linux build installed above, turn that detection off:

```sh
echo 'export SC2_WSL_DETECT=0' >> ~/.bashrc
source ~/.bashrc
```

Without it you'll see warnings about WSL host configuration, and games open in a Windows window.

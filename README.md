# Pre-work tool

`prework.py` sets up your Johto pre-work task in a Docker container (the
"lane"). You run Claude Code in the lane with your own Claude login. Then the
tool collects the diff and the transcript for your Studio task.

## Before you start

You need:

- macOS, Linux, or Windows with WSL. Native Windows is not supported. On
  Windows, open a WSL terminal and work in a folder in your WSL home.
- Python 3.9 or newer (`python3 --version`).
- Git.
- Docker Desktop, OrbStack, colima, or Docker Engine. Start it before setup.
- About 3 GB of free disk: the lane image download (0.7 GB), the image once
  loaded into Docker, and the repository.

## 1. Download and verify the files

1. Open the Setup page on the dashboard: https://cc-johto.vercel.app/setup
2. Make a new, empty folder for your task.
3. Download into it (the page's "Download all" buttons do this in one click):
   - `prework.py`, this `README.md`, and `Dockerfile.lane` (reference only:
     how the lane image was built);
   - the lane image for your computer (about 0.7 GB): `arm64` for an Apple
     Silicon Mac, `amd64` for an Intel Mac or an Intel/AMD Windows or Linux PC.
     If your browser saves it to your Downloads folder, you can leave it there
     (on WSL, setup also looks in your Windows Downloads folder).
4. Check the tool files against the Setup page. In the task folder, paste the
   `shasum -a 256 -c` command from the page; every line must say `OK`. Do not
   run the file if one does not. You don't need to check the image by hand:
   setup checks it before loading it.

## 2. Set up the lane

In the task folder, run:

```sh
python3 prework.py setup
```

Setup asks for two values from your Studio task:

- **Task**: the task name, for example `Task l50u5a0c`.
- **Repo URL**: for example `https://github.com/<owner>/<repo>` or
  `https://github.com/<owner>/<repo>/tree/<commit>`.

You can also give them as flags:

```sh
python3 prework.py setup --task "Task l50u5a0c" --repo https://github.com/<owner>/<repo>
```

If the Repo URL has no commit, setup uses the latest commit on the default
branch and prints it. Use `--commit <40-character sha>` to start from a
different commit.

The first setup on a computer checks the image you downloaded and loads it into
Docker, which takes a minute or two. Later setups on the same computer skip it.

## 3. Do the run

```sh
cd model_a
./claude-dev
```

1. On the first launch, type `/login`. Sign in with your own Claude account.
   The login stays in this task's lane. Your own Claude Code is not changed.
2. Paste your opening prompt and do the run.
3. Finish with `/exit`.

If you close the window or press Ctrl+Z, run `./claude-dev` again. It goes back
to the same session. Dev servers in the lane on ports 3000-3019 and 8000-8009
open on your computer at ports 34000-34019 and 35000-35009.

## 4. Collect the outputs

Go back to the task folder and run:

```sh
cd ..
python3 prework.py collect
```

Collect writes these files to `prework_output/`:

| File | Studio field |
| --- | --- |
| `final.diff` | Final Output |
| the session `.jsonl` transcript | Conversation Log |

Attach both files to your task in Studio. Collect never copies your Claude
login.

## Troubleshooting

- **Docker is not answering.** Start Docker, wait until it is running, then run
  the command again. Your lane and your work are not lost.
- **The lane's ports are taken.** Another pre-work lane uses them. Setup prints
  the `docker stop` command for it. Its work is kept.
- **A new task.** Use a new, empty folder for each task.

## What the tool creates

- In the task folder: `model_a/claude-dev`, `prework-manifest.json`,
  `prework_output/`, and `.prework-*` files.
- In your home folder: `~/.mercor-prework/`. It holds each lane's Claude
  config and login.
- In Docker: the image `mercor-prework-lane`, and containers, volumes and
  networks named `prework-*`.
